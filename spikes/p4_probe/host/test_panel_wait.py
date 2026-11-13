"""Compile/run the real portable wait helper with a fake clock; no IDF build."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class PanelWaitTests(unittest.TestCase):
    def test_link_flags_extract_wrapper_before_dbi_archive(self):
        panel=Path(__file__).resolve().parents[2]/'p4_panel/main'
        flags=re.findall(r'"(-Wl,[^"]+)"',(panel/'CMakeLists.txt').read_text())
        symbol='mipi_dsi_hal_host_gen_read_dcs_command'
        self.assertIn('-Wl,--wrap='+symbol,flags)
        self.assertIn('-Wl,--undefined=__wrap_'+symbol,flags)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            sources={
                'main':'extern "C" int dbi_read(); int main(){return dbi_read()==42?0:1;}\n',
                'wrapper':'extern "C" int __wrap_'+symbol+'(){return 42;}\n',
                'dbi':'extern "C" int '+symbol+'(); extern "C" int dbi_read(){return '+symbol+'();}\n',
            }
            def run(args,**kw):
                return subprocess.run(args,cwd=path,capture_output=True,text=True,timeout=10,**kw)
            for name,source in sources.items():
                (path/(name+'.cpp')).write_text(source)
                run(['c++','-Wall','-Wextra','-Werror','-c',name+'.cpp','-o',name+'.o'],check=True)
            run(['ar','rcs','libwrapper.a','wrapper.o'],check=True)
            run(['ar','rcs','libdbi.a','dbi.o'],check=True)
            command=['c++','main.o','libwrapper.a','libdbi.a','-o','test']
            # The formerly unrooted --wrap link must fail in this archive order.
            failed=run(command+['-Wl,--wrap='+symbol])
            self.assertNotEqual(failed.returncode,0)
            self.assertIn('__wrap_'+symbol,failed.stderr)
            # Use the real spike's link flags; DBI must execute the wrapper.
            run(command+flags,check=True)
            run([str(path/'test')],check=True)

    def test_ready_delayed_stalled_and_shared_deadline(self):
        source=r'''
#include "bounded_wait.h"
#include <cassert>
int main() {
    int64_t now=0;unsigned pauses=0;
    auto clock=[&]{return now;};
    auto pause=[&]{++pauses;++now;};
    assert(panel_probe::wait_ready([]{return true;},clock,pause,5));
    assert(pauses==0);
    assert(panel_probe::wait_ready([&]{return now>=3;},clock,pause,5));
    assert(now==3 && pauses==3);
    // A second phase inherits the original deadline, not a fresh timeout.
    assert(!panel_probe::wait_ready([]{return false;},clock,pause,5));
    assert(now==5 && pauses==5);
    assert(!panel_probe::wait_ready([]{return false;},clock,pause,5));
    assert(pauses==5);
    // A response before the deadline is accepted after yielding to the clock.
    now=0;pauses=0;
    assert(panel_probe::wait_ready([&]{return now>=4;},clock,pause,5));
    assert(now==4 && pauses==4);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'test.cpp').write_text(source)
            include=Path(__file__).resolve().parents[2]/'p4_panel/main'
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-I',str(include),
                            str(path/'test.cpp'),'-o',str(path/'test')],check=True,capture_output=True)
            subprocess.run([str(path/'test')],check=True,capture_output=True,timeout=5)


if __name__=='__main__':unittest.main()
