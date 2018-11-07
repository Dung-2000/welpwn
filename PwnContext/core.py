import os
import shutil
import re
from functools import wraps
from os.path import realpath
from os.path import abspath

# these three does something to construct the PwnContext
from pwnlib.context import _validator
from pwnlib.context import _Tls_DictStack
from pwnlib.context import _defaultdict

from pwn import *
# implemented in proc.py
from proc import Proc

# two important functions
from utils.misc import libc_search, one_gadgets
'''This core script of `PwnContext` has been changed since 2018/11/6, as a result of a sky
size big bro telling me that the usage of the script is awful.
'''


class BetterDict(object):
    '''Just a dict that support a.b <==> a[b]

    Note:
        I didn't add normal dict methods into this class. Use BetterDict._dict directly.
    '''
    def __init__(self, _dict):
        self._dict = _dict

    def __repr__(self):
        return str(self._dict)

    def __str__(self):
        return str(self._dict)

    def __getitem__(self, key):
        return self._dict[key]

    def __iter__(self):
        for key in self._dict:
            yield key

    def __getattr__(self, key):
        if key in self._dict:
            return self._dict[key]


def process_only(method):
    '''just a wrapper to make sure PwnContext.io is an instance of `process` and alive.
    '''
    name = method.__name__

    @wraps(method)
    def wrapper(self, *args, **kargs):
        if not isinstance(self.io, process):
            log.failure('Not a process for {}'.format(name))
            return None
        pid = self.io.pid
        if not os.access('/proc/{}'.format(pid), os.F_OK):
            log.error('process not alive for {}'.format(name))
            return None
        ret_val = method(self, *args, **kargs)
        return ret_val
    return wrapper


class PwnContext(object):
    '''PwnContext is designed for accelerating pwnning by automate many dull jobs.

    Examples:
        >>> ctx = PwnContext()
        >>> ctx.binary = '/bin/sh'
        >>> ctx.remote_libc = './libc.so.6'
        >>> ctx.remote = ('localhost', 1234)
    '''
    defaults = {
                'binary': None,
                'io': None,
                'proc': None,
                'remote': None,
                'remote_libc': None,
                'debug_remote_libc': False,
                'symbols': {},
                'breakpoints': [],
               }

    def __init__(self):
        self._tls = _Tls_DictStack(_defaultdict(PwnContext.defaults))

    @_validator
    def binary(self, binary):
        '''ELF: Binary assigned to the PwnContext.

        Args:
            binary (str of ELF): Path to binary of ELF object.
        '''
        if not binary:
            return None
        if not isinstance(binary, ELF):
            if not os.access(binary, os.R_OK):
                log.failure("Invalid path {} to binary".format(binary))
                return None
            binary = ELF(binary)
        context.binary = binary
        return binary

    @_validator
    def remote_libc(self, libc):
        '''ELF: Remote libc assigned to the PwnContext.
        '''
        if not libc:
            return None
        if not isinstance(libc, ELF):
            if not os.access(libc, os.R_OK):
                log.failure("Invalid path {} to libc".format(libc))
                return None
            libc = ELF(libc)
        return libc

    @_validator
    def io(self, io):
        '''process or remote: IO assigned to the PwnContext.

        Note:
            Generated by PwnContext.start
        '''
        if not isinstance(io, process) and not isinstance(io, remote):
            log.failure("Invalid io {}".format(io))
            return None
        return io

    @_validator
    def symbols(self, symbols):
        '''dict: Symbols to set for gdb. e.g. {'buf':0x202010, }

        Note:
            Only support program (PIE) resolve for now.
        '''
        if not symbols:
            return {}
        assert type(symbols) == dict
        return symbols

    @_validator
    def breakpoints(self, breakpoints):
        '''list: List of breakpoints.

        Note:
            Only support program (PIE) resolve for now.
        '''
        if not breakpoints:
            return []
        assert type(breakpoints) == list
        return breakpoints

    @property
    def libc(self):
        '''ELF: Dynamically find libc. If io is process, return its real libc.
        if is remote, return remote_libc.
        '''
        if isinstance(self.io, remote):
            return self.remote_libc
        elif isinstance(self.io, process):
            return ELF(self.proc.libc)

    @property
    @process_only
    def proc(self):
        '''Proc: Implemented in PwnContext/proc.py
        '''
        return Proc(self.pid)

    @property
    @process_only
    def bases(self):
        '''dict: Dict of vmmap names and its start address.
        '''
        proc = self.proc
        return BetterDict(proc.bases)

    @property
    @process_only
    def canary(self):
        '''int: Canary value of the process.
        '''
        return self.proc.canary

    @property
    @process_only
    def pid(self):
        '''int: pid of the process.
        '''
        return self.io.pid

    def start(self, method='process', **kwargs):
        '''Core method of PwnContext. Handles glibc loading, process/remote generating.

        Args:
            method (str): 'process' will launch a process instance, 'remote' will
            launch a remote instance and 'gdb' will launch process in debug mode.
            **kwargs: arguments to pass to process, remote, or gdb.debug.
        '''
        # checking if there is an open io, then close it.
        if self.io:
            self.io.close()

        if method == 'remote':
            if not self.remote:
                log.error("PwnContext.remote not assigned")
            self.io = remote(self.remote[0], self.remote[1])
            return self.io
        else:
            binary = self.binary
            if not binary:
                log.error("PwnContext.binary not assigned")

            # debug remote libc. be aware that this will use a temp binary
            if self.debug_remote_libc:
                env = {}
                # set LD_PRELOAD
                path = self.remote_libc.path
                if 'env' in kwargs:
                    env = kwargs['env']
                if "LD_PRELOAD" in env and path not in env["LD_PRELOAD"]:
                    env["LD_PRELOAD"] = "{}:{}".format(env["LD_PRELOAD"], path)
                else:
                    env["LD_PRELOAD"] = path
                # log.info("set env={} for debugging remote libc".format(env))

                # codes followed change the ld
                cur_dir = os.path.dirname(os.path.realpath(__file__))
                libc_version = get_libc_version(path)
                arch = ''
                if self.binary.arch == 'amd64':
                    arch = '64'
                elif self.binary.arch == 'i386':
                    arch = '32'
                else:
                    log.error('non supported arch')
                # change the ld for the binary
                ld_path = "{}/libs/libc-{}/{}bit/ld.so.2".format(
                    cur_dir,
                    libc_version,
                    arch)
                shutil.copy(ld_path, '/tmp/ld.so.2')
                binary = change_ld(binary, '/tmp/ld.so.2')

                # set LD_LIBRARY_PATH
                '''Why set LD_LIBRARY_PATH ?
                It's for a future feature. Simply use LD_PRELOAD and change the ld can
                solve many pwn challenges. But there are some challenges not only require
                libc.so.6, but also need libpthread.so......(and other libs).
                I will add all those libs into `PwnContext/libs` to fix this problem.
                '''
                lib_path = "{}/libs/libc-{}/{}bit/".format(
                    cur_dir,
                    libc_version,
                    arch)
                if "LD_LIBRARY_PATH" in env and lib_path not in env["LD_LIBRARY_PATH"]:
                    env["LD_LIBRARY_PATH"] = "{}:{}".format(env["LD_LIBRARY_PATH"], lib_path)
                else:
                    env["LD_LIBRARY_PATH"] = lib_path

                log.info("set env={} for debugging remote libc".format(env))
                kwargs['env'] = env

            if method == 'gdb':
                self.io = binary.debug(**kwargs)
            elif method == 'process':
                self.io = binary.process(**kwargs)
            else:
                log.error('invalid method {}'.format(method))

            return self.io

    @process_only
    def debug(self, **kwargs):
        '''Debug the io if io is an process. Core is to generate gdbscript

        Args:
            **kwargs: args pass to gdb.attach.
        '''
        symbols = self.symbols
        breakpoints = self.breakpoints
        gdbscript = ''
        prog_base = 0
        if self.binary.pie:
            prog_base = self.bases.prog
        for key in symbols:
            gdbscript += 'set ${}={:#x}\n'.format(key, symbols[key] + prog_base)
        for bp in breakpoints:
            gdbscript += 'b *{:#x}\n'.format(bp + prog_base)
        if gdbscript != '':
            log.info('Add gdbscript:\n{}'.format(gdbscript))
            if 'gdbscript' in kwargs:
                kwargs['gdbscript'] += '\n{}'.format(gdbscript)
            else:
                kwargs['gdbscript'] = gdbscript
        return gdb.attach(self.io, **kwargs)

    def __getattr__(self, attr):
        '''This is just a wrapper of ctx.io (process or remote)'''
        if hasattr(self.io, attr):
            method = getattr(self.io, attr)
            if type(method) == 'instancemethod':
                @wraps(method)
                def call(*args, **kwargs):
                    return method(*args, **kwargs)
                return call
            else:
                return method


ctx = PwnContext()


def change_ld(binary, ld):
    '''
    Force to use assigned new ld.so by changing the binary
    '''
    if not os.access(ld, os.R_OK):
        log.failure("Invalid path {} to ld".format(ld))
        return None

    if not isinstance(binary, ELF):
        if not os.access(binary, os.R_OK):
            log.failure("Invalid path {} to binary".format(binary))
            return None
        binary = ELF(binary)

    for segment in binary.segments:
        if segment.header['p_type'] == 'PT_INTERP':
            size = segment.header['p_memsz']
            addr = segment.header['p_paddr']
            data = segment.data()
            if size <= len(ld):
                log.failure("Failed to change PT_INTERP from {} to {}".format(data, ld))
                return None
            binary.write(addr, ld.ljust(size, '\0'))
            if not os.access('/tmp/pwn', os.F_OK):
                os.mkdir('/tmp/pwn')
            path = '/tmp/pwn/{}_debug'.format(os.path.basename(binary.path))
            if os.access(path, os.F_OK):
                os.remove(path)
                info("Removing exist file {}".format(path))
            binary.save(path)
            os.chmod(path, 0b111000000)  # rwx------
    log.success("PT_INTERP has changed from {} to {}. Using temp file {}".format(
        data, ld, path))
    return ELF(path)


def get_libc_version(path):
    '''Get the libc version.

    Args:
        path (str): Path to the libc.
    Returns:
        str: Libc version. Like '2.29', '2.26' ...
    '''
    content = open(path).read()
    pattern = "libc[- ]([0-9]+\.[0-9]+)"
    result = re.findall(pattern, content)
    if result:
        return result[0]
    else:
        return ""
