"""Atomic JSON record writes (step 10 requal-n1 r7/r8): temp file in the same directory, fsync, os.replace,
directory fsync. An interrupted (re)write never destroys the previous record."""
import json
import os


def atomic_write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.tmp-'+str(os.getpid()))
    data=(json.dumps(value,indent=2)+'\n').encode()
    try:
        with open(tmp,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    except BaseException:
        try:os.unlink(tmp)
        except OSError:pass
        raise
    directory=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)
