import subprocess
from pathlib import Path

class MakeTgz(object):
    def __init__(self, source_dir: Path):
        self.zip_exe_path = str(Path(r"C:\Program Files\7-Zip\7z.exe"))
        self.source_dir = source_dir
        self.tar_filename = self.source_dir.parent.joinpath(f'{self.source_dir.name}.tar')

    def run(self, cmd_list: list):
        cmd_list = [self.zip_exe_path, 'a'] + cmd_list
        subprocess.run(cmd_list, check=True)

    def make_tar(self):
        cmd = ['-ttar', str(self.tar_filename), str(self.source_dir)]
        self.run(cmd)

    def create_tgz(self):
        tgz_filename = self.source_dir.parent.joinpath(f'{self.source_dir.name}.tgz')
        cmd = ['-tgzip', str(tgz_filename), str(self.tar_filename)]
        self.run(cmd)

    def __call__(self):
        self.make_tar()
        self.create_tgz()


source_path = Path.cwd().joinpath('tar_gzip_test', 'eax01908702')
MakeTgz(source_path)()

