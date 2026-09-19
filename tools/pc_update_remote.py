#!/usr/bin/env python3
"""Upload the sibling pc_update.py over SSH, run it, and retain console output."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


REMOTE_DIR = '/opt/victronenergy/mqtt-rpc/thirdparty/bslbatt'
SSH_HOST = '172.16.8.66'
SSH_USER = 'root'
SSH_PASSWORD = '12345678'
SSH_PORT = 22


def password_environment(directory):
    """Feed SSH password prompts without putting credentials in argv or logs."""
    helper = Path(directory) / 'askpass.sh'
    helper.write_text('#!/bin/sh\ncase "$1" in\n'
                      '  *[Pp]assword*) printf \'%s\\n\' "$PC_UPDATE_SSH_PASSWORD" ;;\n'
                      '  *) exit 1 ;;\nesac\n', encoding='utf-8')
    helper.chmod(0o700)
    env = os.environ.copy()
    env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force',
               DISPLAY=env.get('DISPLAY') or ':0', PC_UPDATE_SSH_PASSWORD=SSH_PASSWORD)
    return env


def upload_command(directory):
    # Stage on the same filesystem, preserve the previous script, then rename.
    return '\n'.join([
        'set -eu',
        'cd ' + shlex.quote(directory),
        'test -f P41288V110-41289-1.52T-000.bin',
        'stage=$(mktemp ./pc_update.py.upload.XXXXXX)',
        "trap 'rm -f \"$stage\"' EXIT HUP INT TERM",
        'cat > "$stage"',
        'test -s "$stage"',
        "python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding=\"utf-8\").read())' \"$stage\"",
        'chmod 644 "$stage"',
        'if test -f pc_update.py; then cp -p pc_update.py "${stage}.bak"; '
        'printf "Backup: %s/%s.bak\\n" "$PWD" "$stage"; fi',
        'mv -f "$stage" pc_update.py',
        "printf 'Uploaded pc_update.py\\n'",
    ])


def run_command(directory, check_only=False):
    # A lock held by the updater process prevents concurrent runs of this helper.
    code = '\n'.join([
        'import fcntl, runpy, sys',
        "lock = open('/tmp/pc_update_remote.lock', 'a')",
        'try:',
        '    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)',
        'except BlockingIOError:',
        "    sys.exit('Another remote updater is running')",
        "sys.argv = {!r}".format(['pc_update.py', '--check-online'] if check_only else ['pc_update.py']),
        "runpy.run_path('pc_update.py', run_name='__main__')",
    ])
    return 'cd {} && python3 -u -c {}'.format(shlex.quote(directory), shlex.quote(code))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='无参数连接固定目标，覆盖 pc_update.py 并立即执行完整升级。')
    parser.add_argument('host', nargs='?', default=SSH_USER + '@' + SSH_HOST,
                        help='默认使用代码顶部的固定 SSH 账户和地址')
    parser.add_argument('--port', type=int, default=SSH_PORT, help='SSH 端口，默认 22')
    parser.add_argument('--identity', type=Path, help='SSH 私钥文件路径')
    parser.add_argument('--remote-dir', default=REMOTE_DIR)
    parser.add_argument('--check-only', action='store_true', help='上传后仅检查设备在线，不执行升级')
    parser.add_argument('--log-dir', type=Path,
                        default=Path(__file__).resolve().parent.parent / 'logs' / 'pc_update_remote')
    args = parser.parse_args(argv)
    if args.host.startswith('-') or not args.host:
        parser.error('invalid SSH host')
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')

    ssh = ['ssh', '-T', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=3', '-o', 'NumberOfPasswordPrompts=1']
    if args.port is not None:
        ssh += ['-p', str(args.port)]
    if args.identity:
        ssh += ['-i', str(args.identity.expanduser())]
    ssh += [args.host]

    process = None
    try:
        source = Path(__file__).resolve().with_name('pc_update.py')
        payload = source.read_bytes()
        compile(payload, str(source), 'exec')
        args.log_dir.mkdir(parents=True, exist_ok=True)
        path = args.log_dir / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log')
        print('Log: {}'.format(path), flush=True)
        with tempfile.TemporaryDirectory(prefix='pc-update-ssh-') as auth_dir, \
                path.open('w', encoding='utf-8') as log:
            env = password_environment(auth_dir)
            def record(line):
                print(line, end='', flush=True)
                log.write(line)
                log.flush()

            record('Target: {}:{}\nSource: {}\n'.format(args.host, args.remote_dir, source))
            uploaded = subprocess.run(ssh + [upload_command(args.remote_dir)], input=payload,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            record(uploaded.stdout.decode('utf-8', errors='replace'))
            if uploaded.returncode:
                record('Upload failed; updater was not started. exit={}\n'.format(uploaded.returncode))
                return uploaded.returncode
            record('Starting remote {}.\n'.format('online check only' if args.check_only else 'updater (full upgrade)'))
            process = subprocess.Popen(ssh + [run_command(args.remote_dir, args.check_only)],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', env=env)
            for line in process.stdout:
                record(line)
            result = process.wait()
            record('Remote exit={}\n'.format(result))
            if result == 255:
                record('SSH connection failed; remote upgrade state is unknown.\n')
            return result
    except KeyboardInterrupt:
        print('\nInterrupted; remote upgrade state is unknown. Check device/logs before rerunning.',
              file=sys.stderr)
        return 130
    except (OSError, SyntaxError) as exc:
        print('FAILED: {}'.format(exc), file=sys.stderr)
        return 1
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


if __name__ == '__main__':
    sys.exit(main())
