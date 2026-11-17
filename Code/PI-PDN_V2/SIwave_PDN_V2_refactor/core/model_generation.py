"""PDN 대상 부품을 공식 S2P/RLC 코어에 전달하고 위치별 모델 경로를 반환한다."""
import hashlib
import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from core.logger import LogLevel


def _sync_runtime_libraries(result, output, runtime_dirs, *, preserve_unchanged=True):
    """검증된 모델과 위치 매핑을 버전별 라이브러리에 복사하고 경로를 바꾼다."""
    import copy
    from pdn_s2p_core.pmap_core.model_mapping import write_model_mapping
    mapping = json.loads(Path(result['mappingJson']).read_text(encoding='utf-8'))
    job_id = hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:12]
    anchor = output / f'PDN_models.{job_id}.pmap'
    copies, failures, mirrored = [], [], {}
    started = time.perf_counter()
    stats = {'copiedFiles': 0, 'unchangedFiles': 0, 'copiedBytes': 0,
             'writtenMappings': 0, 'unchangedMappings': 0}
    source_hashes = {}
    def digest(path):
        key = str(path.resolve())
        if key not in source_hashes:
            source_hashes[key] = hashlib.sha256(path.read_bytes()).digest()
        return source_hashes[key]
    # Multiple BOM/InnerCap records may select the same model. Mirror one
    # canonical pair so different provenance sidecars cannot rewrite it twice.
    models = {}
    for record in result['records']:
        for variant in record.get('modelVariants', []):
            pn = record['selectedPn']
            if pn in models:
                old = models[pn]
                if any(digest(Path(old[key])) != digest(Path(variant[key])) for key in ('s2pFile', 'rlcFile')):
                    raise ValueError(f'[S2P_GENERATION] Conflicting model variants for shared library PN: {pn}')
            else:
                models[pn] = variant
    for root in map(Path, runtime_dirs):
        records = copy.deepcopy(result['records'])
        folders = set()
        try:
            root.mkdir(parents=True, exist_ok=True)
            for pn, variant in models.items():
                folder = root / pn
                folder.mkdir(parents=True, exist_ok=True)
                source_s2p = Path(variant['s2pFile'])
                for suffix in ('.s2p', '.rlc', '.source.json', '.rlc.source.json', '.mod', '.specification.json'):
                    source = source_s2p.with_suffix(suffix)
                    if not source.is_file():
                        continue
                    target = folder / source.name
                    same_path = source.resolve() == target.resolve()
                    source_sha = digest(source)
                    unchanged = same_path or (preserve_unchanged and target.is_file()
                        and source.stat().st_size == target.stat().st_size
                        and source_sha == hashlib.sha256(target.read_bytes()).digest())
                    if unchanged:
                        stats['unchangedFiles'] += 1
                    else:
                        shutil.copy2(source, target)
                        if source_sha != hashlib.sha256(target.read_bytes()).digest():
                            raise OSError(f'Model copy hash mismatch: {target}')
                        stats['copiedFiles'] += 1
                        stats['copiedBytes'] += source.stat().st_size
            for record in records:
                pn = record.get('selectedPn')
                for variant in record.get('modelVariants', []):
                    folder = root / pn
                    folder.mkdir(parents=True, exist_ok=True)
                    variant.update(s2pFile=str((folder / f'{pn}.s2p').resolve()),
                                   rlcFile=str((folder / f'{pn}.rlc').resolve()),
                                   sourceFile=str((folder / f'{pn}.source.json').resolve()))
                    folders.add(folder)
            if folders:
                # Keep mappings from different input folders; also expose the
                # latest run at the familiar PDN_models.model-mapping.json.
                for destination in [root, *sorted(folders)]:
                    for mapping_anchor in (anchor, output / 'PDN_models.pmap'):
                        saved = write_model_mapping(mapping_anchor, copy.deepcopy(records), mapping,
                                                    destination_dir=destination,
                                                    preserve_unchanged=preserve_unchanged, write_stats=stats)
                        copies.extend(map(str, saved))
                # First configured version is the preferred solver source.
                for record in records:
                    for position in record['scope'].get('pdnPlacements', []):
                        if len(record.get('modelVariants', [])) == 1:
                            mirrored.setdefault(position['designator'], record['modelVariants'][0])
        except OSError as error:
            failures.append(f'{root}: {error}')
    stats['elapsedSeconds'] = time.perf_counter() - started
    return copies, failures, mirrored, stats


def _seed_official_exact_models(parts, libraries, output):
    from pdn_s2p_core.pmap_core.model_service import ModelDownloader
    from pdn_s2p_core.pmap_core.part_rules import get_component_info, normalize_maker_pn
    validator = ModelDownloader(str(output / 'Approved_S2P_Library'))
    for part in parts:
        pn = normalize_maker_pn(part.maker_pn)
        maker = part.manufacturer or get_component_info(pn)[0]
        destination = output / 'Approved_S2P_Library' / pn
        for library in [destination.parent, *map(Path, libraries)]:
            for candidate in library.rglob(f'{pn}.s2p'):
                try:
                    validator.validate_official_pair_folder(candidate.parent, pn, maker)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if candidate.parent.resolve() != destination.resolve():
                    destination.mkdir(parents=True, exist_ok=True)
                    for suffix in ('.s2p', '.rlc', '.source.json', '.rlc.source.json', '.mod', '.specification.json'):
                        source = candidate.with_suffix(suffix)
                        if source.is_file():
                            target = destination / source.name
                            if not target.is_file() or source.read_bytes() != target.read_bytes():
                                shutil.copy2(source, target)
                break
            else:
                continue
            break


def resolve_generation_options(options, input_dir, library_dirs=()):
    resolved = dict(options or {})
    def absolute(value):
        path = Path(value)
        return str(path if path.is_absolute() else Path(input_dir) / path)
    resolved['libraryDirs'] = [absolute(p) for p in [*library_dirs, *resolved.get('libraryDirs', [])] if p]
    resolved['runtimeDirs'] = [absolute(p) for p in resolved.get('runtimeDirs', []) if p]
    if resolved.get('originalSpecificationCatalog'):
        resolved['originalSpecificationCatalog'] = absolute(resolved['originalSpecificationCatalog'])
    return resolved


def generate_pdn_models(*, components, bom_info, inner_cap_audit, output_dir,
                        library_dirs, options, logger=None):
    from pdn_s2p_core import PartEntry, generate_models, part_from_bom
    from pdn_s2p_core.config import ORIGINAL_SPECIFICATION_PATH
    from pdn_s2p_core.pmap_core.part_rules import manufacturer_from_text

    inner = {item['component_name']: item for item in inner_cap_audit}
    metadata = bom_info.get('RefDesModelInputs', {})
    # Older preprocessing_result.json may only contain the column arrays.
    if not metadata:
        for index, group in enumerate(bom_info.get('Designator', [])):
            def value(column):
                values = bom_info.get(column, [])
                return str(values[index] or '') if index < len(values) else ''
            for name in str(group).split(','):
                metadata[name.strip()] = {
                    'technical_spec': value('TechnicalSpec'),
                    'manufacturer': value('Manufacturer'),
                }
    grouped = {}
    for name, component in sorted(components.items()):
        if name in inner:
            item = inner[name]
            pn = str(item.get('maker_part_number') or item.get('part_number') or '')
            part = PartEntry('', pn, False, source_spec=str(item.get('cap_value', '')),
                             manufacturer=str(item.get('vendor', '')))
        else:
            item = metadata.get(name, {})
            pn = str(bom_info.get('RefDesToMakerPartNumber', {}).get(name, ''))
            lge = str(bom_info.get('RefDesToPartNumber', {}).get(name, ''))
            spec = str(item.get('technical_spec', ''))
            part = part_from_bom(lge, spec, item.get('manufacturer', '')) if spec else PartEntry(
                lge, pn, True, manufacturer=str(item.get('manufacturer', '')))
            if not part.maker_pn:
                part.maker_pn = pn
        canonical_maker = manufacturer_from_text(part.manufacturer)
        if canonical_maker:
            part.raw_manufacturer = part.raw_manufacturer or part.manufacturer
            part.manufacturer = canonical_maker
        # Include every instance in the API's per-PN deduplicated record.
        key = (part.lge_pn, part.raw_maker_pn or part.maker_pn, part.is_bom)
        if key not in grouped:
            grouped[key] = part
        else:
            previous = grouped[key]
            if previous.source_spec != part.source_spec:
                raise ValueError(f'[S2P_GENERATION] Conflicting specifications for {part.maker_pn}: {name}')
        for pin, pin_obj in component.pins.items():
            grouped[key].pdn_placements.append({'designator': name, 'pin': str(pin),
                                                'rail': str(pin_obj.net_name or '')})

    output = Path(output_dir) / 'pdn_models'
    runtime_dirs = list(dict.fromkeys(str(Path(p).resolve()) for p in options.get('runtimeDirs', [])))
    paths = list(dict.fromkeys(str(Path(p).resolve()) for p in [*runtime_dirs, *library_dirs] if p))
    catalog = options.get('originalSpecificationCatalog') or str(ORIGINAL_SPECIFICATION_PATH)
    request = {'parts': [asdict(p) for p in grouped.values()], 'libraryDirs': paths,
               'allowNetwork': options.get('allowNetwork', True),
               'originalSpecificationCatalog': catalog,
               'catalogSha256': hashlib.sha256(Path(catalog).read_bytes()).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = output / 'generation_request.json'
    result_file = output / 'PDN_models.result.json'
    result = None
    # PRE snapshots and Step 5 can call this twice in one run. Reuse only a
    # completed request whose selected model bytes still match the core proof.
    if cache.is_file() and result_file.is_file():
        cached = json.loads(cache.read_text(encoding='utf-8'))
        previous = json.loads(result_file.read_text(encoding='utf-8'))
        if cached.get('fingerprint') == fingerprint and not previous.get('modelPending') and not previous.get('mappingWriteFailures'):
            variants = [v for r in previous['records'] for v in r.get('modelVariants', [])]
            hashes = cached.get('modelFileHashes', {})
            if hashes and all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in hashes.items()) and all(Path(v['rlcFile']).is_file() and Path(v['s2pFile']).is_file()
                   and hashlib.sha256(Path(v['s2pFile']).read_bytes()).hexdigest() == v['s2pSha256'] for v in variants):
                result = previous
    if result is None:
        # The exported core downloads before its library fallback. Seed only
        # fully validated official exact pairs so saved models take priority.
        _seed_official_exact_models(grouped.values(), paths, output)
        def log(message):
            if logger:
                logger.log(f'[S2P_GENERATION] {message}', level=LogLevel.INFO)
        result = generate_models(grouped.values(), output, library_dirs=paths,
                                 allow_network=request['allowNetwork'], filter_pdn_scope=False,
                                 original_specification_catalog=catalog, log=log)
        hashes = {}
        for record in result['records']:
            for variant in record.get('modelVariants', []):
                files = [Path(variant[field]) for field in ('s2pFile', 'rlcFile', 'sourceFile')]
                files.append(Path(variant['rlcFile']).with_suffix('.rlc.source.json'))
                for path in files:
                    if path.is_file():
                        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        cache.write_text(json.dumps({'fingerprint': fingerprint, **request, 'modelFileHashes': hashes}, ensure_ascii=False, indent=2), encoding='utf-8')

    # Synchronize even on a cache hit: deleted mirrors or mappings must be
    # restored without needing another vendor request.
    runtime_copies, runtime_failures, mirrored, sync_stats = _sync_runtime_libraries(result, output, runtime_dirs)
    selected, pending = {}, []
    for record in result['records']:
        variants = record.get('modelVariants', [])
        names = {p['designator'] for p in record['scope'].get('pdnPlacements', [])}
        if record['status'] not in ('exact_model_selected', 'selected_bom_spec_substitute') or len(variants) != 1:
            pending.extend(names)
            continue
        variant = variants[0]
        if not all(Path(variant[field]).is_file() for field in ('s2pFile', 'rlcFile', 'sourceFile')):
            pending.extend(names)
            continue
        for name in names:
            selected[name] = {'originalPn': record['originalPn'], 'selectedPn': record['selectedPn'],
                              'status': record['status'], 'mappingFile': result['mappingJson'],
                              'generatedS2pFile': variant['s2pFile'],
                              **mirrored.get(name, variant)}
    summary = {'exactModels': result['exactModels'], 'substituteModels': result['substituteModels'],
               'pendingComponents': sorted(pending), 'selectedComponents': selected,
               'mappingJson': result['mappingJson'], 'resultJson': str(result_file),
               'mappingWriteFailures': result.get('mappingWriteFailures', []),
               'runtimeDirs': runtime_dirs, 'runtimeMappingCopies': runtime_copies,
               'runtimeCopyFailures': runtime_failures, 'runtimeSyncStats': sync_stats}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'pdn_model_generation_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if logger:
        logger.log(f"[S2P_GENERATION] exact={result['exactModels']}, substitute={result['substituteModels']}, pending_components={len(pending)}", level=LogLevel.INFO)
        logger.log(f"[S2P_GENERATION][LIBRARY_SYNC] seconds={sync_stats['elapsedSeconds']:.3f}, copied_files={sync_stats['copiedFiles']}, unchanged_files={sync_stats['unchangedFiles']}, written_mappings={sync_stats['writtenMappings']}, unchanged_mappings={sync_stats['unchangedMappings']}", level=LogLevel.INFO)
    if runtime_failures and options.get('requireRuntimeCopies', True):
        raise RuntimeError(f'[S2P_GENERATION] Ansys library synchronization failed; check write permissions. See {output / "pdn_model_generation_summary.json"}')
    if (pending or summary['mappingWriteFailures']) and options.get('requireAllModels', True):
        raise RuntimeError(f'[S2P_GENERATION] Model generation incomplete; solve stopped. See {output / "pdn_model_generation_summary.json"}')
    return selected, summary
