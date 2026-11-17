"""PDN 자동화 또는 PowerShell에서 사용할 CLI."""
import argparse
import json
from pathlib import Path

from .api import generate_models


def main():
    parser = argparse.ArgumentParser(description='GUI 없이 공식 S2P·RLC·대체 모델 매핑을 확보합니다.')
    parser.add_argument('--input', type=Path, required=True, help='PartEntry 필드로 구성된 JSON parts 목록')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pdn-spec', type=Path)
    parser.add_argument('--ndf', type=Path)
    parser.add_argument('--library', type=Path, action='append', default=[])
    parser.add_argument('--runtime-dir', type=Path, action='append', default=[])
    parser.add_argument('--original-specifications', type=Path)
    parser.add_argument('--already-pdn-filtered', action='store_true')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    content = json.loads(args.input.read_text(encoding='utf-8-sig'))
    result = generate_models(content['parts'], args.output, analysis_spec=args.pdn_spec,
        ndf_file=args.ndf, policy=content.get('policy'), library_dirs=args.library,
        runtime_dirs=args.runtime_dir, allow_network=not args.offline,
        filter_pdn_scope=not args.already_pdn_filtered,
        original_specification_catalog=args.original_specifications, log=print)
    print(json.dumps({k: result[k] for k in ('exactModels', 'substituteModels', 'modelPending',
        'purposeReview', 'excludedNonPdn', 'mappingJson', 'mappingCsv')}, ensure_ascii=False, indent=2))
    return 1 if result['modelPending'] or result['mappingWriteFailures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
