"""EDEN/DCG entry point for the SI-TDR customer package."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence

from core.runtime import main as run_si_tdr


def main(argv: Sequence[str] | None = None) -> int:
    """Forward the external Heaven request path and options to the runtime."""

    return run_si_tdr(argv)


if __name__ == "__main__":
    exit_code = main()
    # EDEN은 종료 코드로 성공을 판정한다. pythonnet/CLR은 인터프리터 종료
    # 루틴에서 간헐적으로 fatal AccessViolation을 일으켜, 모든 작업이 성공한
    # 뒤에도 프로세스를 비정상 종료시킬 수 있다 (.NET Runtime event 1026,
    # 2026-08-19 관측). 로그를 확정한 뒤 종료 루틴을 건너뛰고 코드를 직접
    # 확정한다. 이 시점에는 Ansys 세션과 publish가 모두 끝나 있다.
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(exit_code))
