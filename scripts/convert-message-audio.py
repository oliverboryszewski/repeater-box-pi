#!/usr/bin/python3
"""Apply decoder resource limits, then replace this unprivileged worker with FFmpeg."""
import os
from pathlib import Path
import resource
import stat
import sys

FFMPEG = '/usr/bin/ffmpeg'
FORMATS = {'wav': 'wav', 'mp3': 'mp3', 'm4a': 'mov'}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def command(extension, source, destination):
    if extension not in FORMATS:
        raise ValueError('Unsupported audio format')
    arguments = [FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
                 '-max_alloc', str(64 * 1024 * 1024), '-filter_threads', '1',
                 '-filter_complex_threads', '1', '-protocol_whitelist', 'file',
                 '-threads', '1', '-f', FORMATS[extension]]
    if extension == 'm4a':
        arguments += ['-enable_drefs', '0', '-use_absolute_path', '0']
    return arguments + ['-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-map_metadata', '-1', '-map_chapters', '-1', '-af',
        'aresample=16000,atrim=end_sample=480001', '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', '-threads', '1', '-f', 'wav', '-y', str(destination)]


def limit_resources():
    def cap(which, soft, hard=None):
        _old_soft, old_hard = resource.getrlimit(which)
        hard = soft if hard is None else hard
        if old_hard != resource.RLIM_INFINITY:
            hard = min(hard, old_hard)
        resource.setrlimit(which, (min(soft, hard), hard))
    cap(resource.RLIMIT_CORE, 0)
    cap(resource.RLIMIT_AS, 384 * 1024 * 1024)
    cap(resource.RLIMIT_CPU, 12, 15)
    cap(resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES)
    cap(resource.RLIMIT_NOFILE, 64)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3 or arguments[0] not in FORMATS or os.geteuid() == 0:
        raise ValueError('Use the unprivileged audio conversion worker')
    extension, source, destination = arguments[0], Path(arguments[1]), Path(arguments[2])
    if not source.is_absolute() or not destination.is_absolute() or source.parent != destination.parent:
        raise ValueError('Invalid conversion workspace')
    folder = source.parent.lstat()
    info = source.lstat()
    if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.geteuid() or folder.st_mode & 0o077
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not 0 < info.st_size <= MAX_UPLOAD_BYTES
            or destination.exists() or destination.is_symlink()):
        raise ValueError('Invalid conversion workspace')
    limit_resources()
    os.umask(0o077)
    os.execv(FFMPEG, command(extension, source, destination))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        sys.exit('Audio conversion could not be prepared.')
