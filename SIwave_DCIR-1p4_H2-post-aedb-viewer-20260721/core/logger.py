# coding=utf-8
# © <2025>ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import logging
import os
import sys
import time
from enum import Enum
from pathlib import Path
from colorama import init as colorama_init, Fore, Style

colorama_init(autoreset=True)


class LogLevel(Enum):
    FATAL = -2
    SECTION = -1
    ERROR = 0
    INFO = 1
    DETAIL1 = 2
    DETAIL2 = 3
    DETAIL3 = 4
    DETAIL4 = 5
    DETAIL5 = 6
    DETAIL6 = 7
    WARNING = 8  # [수정] 에러 해결을 위해 WARNING 레벨 추가
    DEBUG = 9    # [수정] ERROR(0)와의 값 중복을 피하기 위해 9로 변경


class Logger:
    def __init__(self, name: str = "app", log_dir: str = "."):
        self.name = name
        self.log_dir = log_dir
        self._log_buffer = []
        self._formatter = time.strftime('%Y/%m/%d %H:%M:%S')
        self._pre_level_value = 1

    def log(self, msg: str, level: LogLevel = LogLevel.INFO, line_change: bool = False, enable_indent: bool = True):
        # [수정] WARNING 레벨일 때도 ERROR와 동일한 구분자(!)를 사용하도록 추가
        if level == LogLevel.ERROR or level == LogLevel.WARNING:
            diff_char = '!'
        else:
            diff_char = '>'
            
        # [수정] WARNING과 DEBUG의 값이 커서 들여쓰기가 너무 깊어지는 것을 방지
        indent_value = level.value
        if level == LogLevel.WARNING or level == LogLevel.DEBUG:
            indent_value = 0  # ERROR와 동일한 들여쓰기 수준 적용
            
        if enable_indent:
            indent = '\t' * (indent_value + 1) + diff_char * (indent_value - 1) + ' ' if indent_value >= 0 else '\t' * (indent_value + 1)
        else:
            indent = '\t' * (indent_value + 1) if indent_value >= 0 else '\t' * (indent_value + 1)
            
        timestamp = time.strftime('%Y/%m/%d %H:%M:%S')

        if level == LogLevel.FATAL:
            log_line = f"\n{msg}"
            self._log_buffer.append(log_line)
            print(self._colorize(log_line, level))
            self.save(self.log_dir)
            sys.exit(msg)

        elif level == LogLevel.SECTION:
            if line_change:
                log_line = f"\n{msg}"  # [수정] 기존 코드의 UnboundLocalError 버그 수정 (log_line -> msg)
            else:
                log_line = f"{indent}{msg}"

        elif level == LogLevel.ERROR or level == LogLevel.WARNING:
            log_line = f"{timestamp}{indent}{msg}"
            if line_change:
                log_line = f"\n{log_line}"

        else:
            log_line = f"{timestamp}{indent}{msg}"
            if line_change:
                log_line = f"\n{log_line}"

        self._log_buffer.append(log_line)
        print(self._colorize(log_line, level))
        self._pre_level_value = level.value

    def fatal(self, msg: str):
        self.log(msg, level=LogLevel.FATAL)

    def save(self, path: str = None):
        if path is None:
            path = self.log_dir
        os.makedirs(path, exist_ok=True)  # auto create directory if not exists
        filename = f"{self.name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
        filepath = Path(os.path.join(path, filename))
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self._log_buffer))
        self.log(f"Save Log File : {filepath.as_posix()}", level=LogLevel.DEBUG)
        self.log("[END]", level=LogLevel.DEBUG)

    @property
    def log_dir(self):
        return self._log_dir

    @log_dir.setter
    def log_dir(self, value):
        self._log_dir = value

    @staticmethod
    def _colorize(msg: str, level: LogLevel) -> str:
        if level == LogLevel.FATAL:
            return f"{Fore.RED}{msg}{Style.RESET_ALL}"
        elif level == LogLevel.ERROR:
            return f"{Fore.RED}{msg}{Style.RESET_ALL}"
        elif level == LogLevel.WARNING:
            return f"{Fore.YELLOW}{msg}{Style.RESET_ALL}"  # [수정] WARNING 출력 색상 추가
        elif level == LogLevel.SECTION:
            return f"{Fore.YELLOW}{msg}{Style.RESET_ALL}"
        elif level == LogLevel.DEBUG:
            return f"{Fore.BLUE}{msg}{Style.RESET_ALL}"
        elif level == LogLevel.INFO:
            return f"{Fore.GREEN}{msg}{Style.RESET_ALL}"
        else:
            return f"{Fore.WHITE}{msg}{Style.RESET_ALL}"