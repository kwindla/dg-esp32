"""Host-only console vocabulary from firmware source; never access a device.

Models top-level names and the demo dispatch grammar. Target capability,
runtime state and other commands' argument semantics remain separate checks.
"""
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ConsoleCommands:
    names: frozenset
    demo_exact: frozenset
    demo_prefix: frozenset

    @classmethod
    def from_firmware(cls, root=ROOT):
        source=(Path(root)/'main/console.cpp').read_text()
        pattern=r'std::strcmp\(\s*command\s*,\s*"([^"]+)"\s*\)'
        names=frozenset(re.findall(pattern,source))
        # Restrict action parsing to the actual demo dispatch, not gate/disp.
        start=re.search(r'else\s+if\s*\(!std::strcmp\(command,"demo"\)\)\s*\{',source)
        if start is None:raise ValueError('firmware demo dispatch not found')
        rest=source[start.end():]
        end=re.search(r'else\s+if\s*\(!std::strcmp\(command,',rest)
        if end is None:raise ValueError('firmware demo dispatch end not found')
        demo=rest[:end.start()]
        exact=frozenset(re.findall(r'std::strcmp\(action,"([^"]+)"\)',demo))
        prefix=frozenset(re.findall(r'std::strncmp\(action,"([^"]+)",\d+\)',demo))
        if not names or not exact or not prefix:raise ValueError('incomplete firmware console vocabulary')
        return cls(names,exact,prefix)

    def validate(self, text, *, partial=False):
        words=text.split(maxsplit=1)
        if not words or words[0] not in self.names:
            raise ValueError('unsupported firmware console command: '+text)
        if words[0]!='demo':return
        action=words[1] if len(words)==2 else ''
        if action in self.demo_exact:return
        if any(action.startswith(prefix) for prefix in self.demo_prefix):
            if partial:return
            # The sole current prefix action is configure JSON. Keep its
            # required types consistent with console.cpp's data.at/get calls.
            data=json.loads(action.split(maxsplit=1)[1])
            if (type(data.get('url')) is str and type(data.get('volume_db')) is int and
                    type(data.get('enabled')) is bool):return
            raise ValueError('invalid firmware demo configure fields')
        raise ValueError('unsupported firmware demo action: '+action)


def literal_commands(path):
    """Console-call literals/prefixes; dynamic expressions are explicitly listed.

    This is an audit aid, not Python evaluation. Loops/tables and forwarded
    variables require reviewing their declarations at the returned locations.
    """
    tree=ast.parse(Path(path).read_text());found=[];dynamic=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):continue
        index=None
        if isinstance(node.func,ast.Attribute) and node.func.attr in ('command','send'):index=0
        elif isinstance(node.func,ast.Name) and node.func.id in ('console_command','response','issue','command_rows'):index=1
        if index is None or len(node.args)<=index:continue
        value=node.args[index];text=None;partial=False
        if isinstance(value,ast.Constant) and isinstance(value.value,str):text=value.value
        elif isinstance(value,ast.JoinedStr):
            partial=True
            if value.values and isinstance(value.values[0],ast.Constant):text=value.values[0].value
        elif isinstance(value,ast.BinOp) and isinstance(value.op,ast.Add) and isinstance(value.left,ast.Constant) and isinstance(value.left.value,str):
            text=value.left.value;partial=True
        if text is None:dynamic.append(dict(line=node.lineno,expression=ast.unparse(value)))
        else:found.append(dict(line=node.lineno,text=text,partial=partial))
    return found,dynamic
