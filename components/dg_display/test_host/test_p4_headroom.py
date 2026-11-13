"""The link gate must reject pre-main RAM loss, internal storage and ISR drift."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('headroom',ROOT/'scripts/check_p4_headroom.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class Headroom(unittest.TestCase):
    def layout(self):
        return dict(machine='EM_RISCV',heap_start_low=guard.RETENTION_END-116416,heap_start_high=0x4ff40000,
            panel_storage_bytes=17632,panel_storage_section='.ext_ram.bss',panel_storage_type='SHT_NOBITS',
            dsi_isr_section='.iram0.text',backend_global_constructor=False)
    def test_declared_budget_passes_with_masked_interrupt_context(self):
        report=guard.evaluate(self.layout());self.assertTrue(report['passed'])
        self.assertEqual(report['expected_boot_retention_kib'],113)
        self.assertEqual(report['minimum_bytes'],119024-4096)
        self.assertFalse(report['runtime_boot_verified'])
    def test_exact_floor_passes_and_one_byte_below_fails(self):
        row=self.layout();row['heap_start_low']=guard.RETENTION_END-guard.MIN_RETENTION_BYTES
        guard.evaluate(row);row['heap_start_low']+=1
        with self.assertRaisesRegex(ValueError,'headroom'):guard.evaluate(row)
    def test_observed_boot_loop_retention_budget_fails(self):
        row=self.layout();row['heap_start_low']=0x4ff22dc0
        with self.assertRaisesRegex(ValueError,'98816 B below'):guard.evaluate(row)
    def test_wrong_layout_storage_and_placement_fail_closed(self):
        for key,value,reason in (('machine','EM_XTENSA','RISC-V'),('heap_start_high',0x4ff50000,'layout'),
            ('panel_storage_section','.dram0.bss','external BSS'),('panel_storage_type','SHT_PROGBITS','external BSS'),
            ('panel_storage_bytes',0,'external BSS'),('dsi_isr_section','.flash.text','IRAM'),
            ('backend_global_constructor',True,'constructor')):
            with self.subTest(key=key):
                row=self.layout();row[key]=value
                with self.assertRaisesRegex(ValueError,reason):guard.evaluate(row)


class ElfReader(unittest.TestCase):
    """Build tiny debug ELFs; exercise real symbol/section/pointer extraction."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='p4-headroom-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.compiler=shutil.which('riscv32-esp-elf-g++')
        self.assertIsNotNone(self.compiler,'source the pinned IDF export before host tests')

    def fixture(self, *, retention=116160, storage=True, heap=True, backend=None):
        common=self.root/'common.cpp'
        common.write_text('''
namespace dg_display { namespace {
char panel_storage[32] __attribute__((section(".ext_ram.bss"), used));
} }
extern "C" void __attribute__((section(".iram0.text"))) mipi_dsi_dma_trans_done_cb() {}
int common_init() { return 9; }
int common_state=common_init();
'''.replace('panel_storage','panel_storage' if storage else 'other_storage'))
        inputs=[str(common)]
        if backend:
            source=self.root/'backends/jd9365_dsi'/backend;source.parent.mkdir(parents=True,exist_ok=True)
            # The initializer's GCC name follows helper_before, not panel_backend.
            source.write_text('namespace dg_display { void helper_before() {} int init() { return 7; } int state=init(); }\n')
            inputs.append(str(source))
        linker=self.root/'fixture.ld'
        linker.write_text('''SECTIONS {
. = 0x48000000;
.text : { *(.text*) }
.flash.init_array ALIGN(4) : {
__preinit_array_start = .; KEEP(*(.preinit_array)) __preinit_array_end = .;
__init_array_start = .; KEEP(*(.init_array*)) __init_array_end = .;
/* Mimic the real IDF table's unrelated trailing data. */
LONG(0x12345678) LONG(0x10001)
}
.iram0.text 0x4ff00000 : { *(.iram0.text) }
.ext_ram.bss 0x48280000 (NOLOAD) : { *(.ext_ram.bss) }
HEAP_START = HEAP_VALUE;
_heap_start_high = 0x4ff40000;
}'''.replace('HEAP_START','_heap_start_low' if heap else '_wrong_heap_start')
            .replace('HEAP_VALUE',hex(guard.RETENTION_END-retention)))
        elf=self.root/'fixture.elf'
        subprocess.run([self.compiler,'-g','-march=rv32imac','-mabi=ilp32','-fno-exceptions','-fno-rtti',
            '-nostdlib','-Wl,--build-id=none','-Wl,-T,'+str(linker),*inputs,'-o',str(elf)],
            check=True,capture_output=True,text=True)
        return elf

    def test_real_elf_reader_resolves_only_executed_constructor_pointers(self):
        result=guard.inspect_elf(self.fixture())
        self.assertEqual(result['retention_bytes'],116160)
        self.assertEqual(result['psram_panel_storage_bytes'],32)
        self.assertEqual(len(result['pre_main_constructors']),1)
        self.assertTrue(any('common.cpp:' in source for source in result['pre_main_constructors'][0]['sources']))

    def test_floor_specific_negative_elf_has_valid_storage_isr_and_constructors(self):
        elf=self.fixture(retention=guard.MIN_RETENTION_BYTES-1)
        with self.assertRaisesRegex(ValueError,'114927 B below 114928 B'):guard.inspect_elf(elf)
        with patch.object(guard,'MIN_RETENTION_BYTES',0):
            self.assertTrue(guard.inspect_elf(elf)['passed'])

    def test_missing_storage_is_extracted_as_zero_and_rejected(self):
        with self.assertRaisesRegex(ValueError,'external BSS'):guard.inspect_elf(self.fixture(storage=False))

    def test_heap_symbol_extraction_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'missing P4 rev<3 heap'):guard.inspect_elf(self.fixture(heap=False))

    def test_constructor_rejected_by_source_even_with_different_gcc_symbol_name(self):
        for name in ('backend.cpp','framebuffer.cpp','runtime.cpp'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError,'pre-main constructor'):guard.inspect_elf(self.fixture(backend=name))


if __name__=='__main__':unittest.main()
