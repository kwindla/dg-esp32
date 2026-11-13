"""Pure CMake selector controls; run only after the coordinator frees the bench."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class AecArchiveProfileTests(unittest.TestCase):
    def resolve(self,target,less_v3,major,expected=None):
        temporary=Path(os.environ.get('TMPDIR') or Path.home()/'.cache/gap3-tmp')
        temporary.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='aec-profile-',dir=temporary) as directory:
            script=Path(directory)/'profile.cmake'
            code=f'include("{ROOT}/cmake/aec_archive_profiles.cmake")\n'
            code+=f'dg_aec_archive_profile("{target}" "{less_v3}" "{major}" profile)\n'
            if expected:
                for field,value in expected.items():
                    code+=f'if(NOT "${{profile_{field}}}" STREQUAL "{value}")\nmessage(FATAL_ERROR "Unexpected {field}")\nendif()\n'
            script.write_text(code)
            return subprocess.run(['cmake','-P',str(script)],capture_output=True,text=True,timeout=10)

    def test_s3_path_and_qualified_hash_are_unchanged(self):
        result=self.resolve('esp32s3','',6,dict(KEY='esp32s3_idf6',DIRECTORY='esp32s3',BOARD='echoear',
            HASH='670639c3d136c7e3909aa90bcaac573600b7e98b6850be8fd5484b4cdea96dcc'))
        self.assertEqual(result.returncode,0,result.stderr)

    def test_p4_less_v3_selects_step1b_hash(self):
        result=self.resolve('esp32p4','y',6,dict(KEY='esp32p4_less_v3_idf6',DIRECTORY='esp32p4_less_v3_idf6',BOARD='p4xc',
            HASH='16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100'))
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unknown_target_revision_and_idf_major_are_rejected(self):
        for target,revision,major in [('esp32p4','n',6),('esp32s3','y',6),('esp32','',6),
            ('esp32p4','y',7),('esp32s3','',5),('esp32s3','', '')]:
            with self.subTest(target=target,revision=revision,major=major):
                result=self.resolve(target,revision,major)
                self.assertNotEqual(result.returncode,0)
                self.assertRegex(result.stderr,'Unaudited AEC archive key|Unsupported AEC target/revision|explicit IDF major')

if __name__=='__main__':unittest.main()
