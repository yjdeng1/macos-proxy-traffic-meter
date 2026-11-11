#!/usr/bin/env python3
"""Build and install the meter for the current macOS user."""
import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parent
BUNDLE = 'dev.proxytraffic.meter'
SAMPLER = BUNDLE + '.sampler'
UI = BUNDLE + '.ui'
FILES = ('traffic_guard.py', 'proxy_accounting.py', 'traffic_history.py', 'settings.py')


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.next')
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def validate_app(app):
    if not app.exists():
        return
    info = app / 'Contents/Info.plist'
    if not info.exists() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') != BUNDLE:
        raise RuntimeError('The destination contains another application; choose a different --apps-dir')


def stop_job(label, runtime, app):
    domain = f'gui/{os.getuid()}/{label}'
    result = subprocess.run(['launchctl', 'print', domain], capture_output=True, text=True)
    if result.returncode:
        return
    match = re.search(r'\bpid = (\d+)', result.stdout)
    if match:
        pid = int(match[1])
        receipt_path = runtime / 'data' / ('daemon.pid.json' if label == SAMPLER else 'menu.pid.json')
        receipt = json.loads(receipt_path.read_text())
        if pid <= 1 or receipt.get('pid') != pid:
            raise RuntimeError('Refusing to stop an unverified process')
        identity = subprocess.check_output(
            ['ps', '-p', str(pid), '-o', 'uid=,args='], text=True).strip().split(None, 1)
        expected = str(runtime/'traffic_guard.py')+' run' if label == SAMPLER \
            else str(app/'Contents/MacOS/ProxyTraffic')
        if (len(identity) != 2 or identity[0] != str(os.getuid()) or
                not identity[1].endswith(expected)):
            raise RuntimeError('Refusing to stop a process outside this installation')
    subprocess.run(['launchctl', 'bootout', domain], check=True)


def launch_agent(label, arguments, runtime, keep_alive):
    prefix = 'sampler' if label == SAMPLER else 'menu'
    return dict(Label=label, ProgramArguments=list(map(str, arguments)), RunAtLoad=True,
                KeepAlive=keep_alive, WorkingDirectory=str(runtime),
                LimitLoadToSessionType='Aqua', ThrottleInterval=5,
                EnvironmentVariables={'PYTHONDONTWRITEBYTECODE': '1'},
                StandardOutPath=str(runtime/'data'/f'{prefix}.stdout.log'),
                StandardErrorPath=str(runtime/'data'/f'{prefix}.stderr.log'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    controller = parser.add_mutually_exclusive_group()
    controller.add_argument('--controller-socket')
    controller.add_argument('--controller-url')
    parser.add_argument('--secret-env', help='Read controller secret from this environment variable')
    parser.add_argument('--timezone', default=None, help='IANA timezone; first-install default: UTC')
    parser.add_argument('--proxy-ports', default=None, help='Comma-separated local proxy ports')
    parser.add_argument('--runtime', type=Path,
                        default=Path.home()/'Library/Application Support/ProxyTrafficMeter')
    parser.add_argument('--apps-dir', type=Path, default=Path.home()/'Applications')
    parser.add_argument('--agents-dir', type=Path, default=Path.home()/'Library/LaunchAgents')
    parser.add_argument('--no-start', action='store_true', help='Build and configure without starting services')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--purge', action='store_true', help='Also remove this installation runtime on uninstall')
    args = parser.parse_args()
    if platform.system() != 'Darwin' or sys.version_info < (3, 10):
        parser.error('Requires macOS and Python 3.10 or newer')
    runtime = args.runtime.expanduser().resolve()
    app = args.apps_dir.expanduser().resolve()/'Proxy Traffic.app'
    agents = args.agents_dir.expanduser().resolve()
    if os.getuid() == 0:
        parser.error('Run as your normal logged-in user, without sudo')
    if runtime in (Path('/'), Path.home(), SOURCE):
        parser.error('Choose a dedicated runtime directory outside the source checkout')
    validate_app(app)
    if args.uninstall:
        manifest_path = runtime/'installation.json'
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get('bundle') != BUNDLE or manifest.get('runtime') != str(runtime)
                or manifest.get('app') != str(app)):
            parser.error('Installation manifest does not match these paths')
        for label in (UI, SAMPLER):
            stop_job(label, runtime, app)
        for label in (UI, SAMPLER):
            (agents/(label+'.plist')).unlink(missing_ok=True)
        if app.exists():
            shutil.rmtree(app)
        if args.purge:
            shutil.rmtree(runtime)
        else:
            print('Configuration and local history retained at:', runtime)
        print('Uninstalled.')
        return
    if args.no_start:
        for label in (UI, SAMPLER):
            loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                                    capture_output=True, text=True)
            if loaded.returncode == 0:
                parser.error('--no-start requires an installation without loaded jobs')
    config_path = runtime/'config.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if args.controller_socket:
        config.pop('controller_url', None)
        config['controller_socket'] = str(Path(args.controller_socket).expanduser())
    elif args.controller_url:
        config.pop('controller_socket', None)
        config['controller_url'] = args.controller_url
    elif not config:
        parser.error('Specify --controller-url or --controller-socket on first installation')
    config['timezone'] = args.timezone or config.get('timezone', 'UTC')
    ZoneInfo(config['timezone'])
    config['proxy_ports'] = ([int(value) for value in args.proxy_ports.split(',')]
                             if args.proxy_ports else config.get('proxy_ports', [7890, 7897]))
    if not config['proxy_ports'] or any(not 1 <= p <= 65535 for p in config['proxy_ports']):
        parser.error('Proxy ports must be between 1 and 65535')
    if args.secret_env:
        if args.secret_env not in os.environ:
            parser.error('The requested secret environment variable is not set')
        config['controller_secret'] = os.environ[args.secret_env]
    elif 'controller_secret' not in config:
        if not sys.stdin.isatty():
            parser.error('Use --secret-env in non-interactive installs (empty value is allowed)')
        config['controller_secret'] = getpass.getpass('Clash controller secret (empty if disabled): ')
    from traffic_guard import Controller
    Controller(config)  # Validate without contacting or changing the controller.
    if not shutil.which('swiftc') or not shutil.which('codesign'):
        parser.error('Install Xcode Command Line Tools first: xcode-select --install')
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    with tempfile.TemporaryDirectory(prefix='proxy-traffic-build-') as temporary:
        build = Path(temporary)
        cache = build/'module-cache'
        for source, binary in [('traffic_alert.swift', 'TrafficGuardAlert'),
                               ('proxy_traffic_menu.swift', 'ProxyTraffic')]:
            subprocess.run(['swiftc', '-O', '-target', platform.machine()+'-apple-macosx13.0',
                            '-module-cache-path', str(cache), '-framework', 'AppKit',
                            '-framework', 'SwiftUI', str(SOURCE/source),
                            '-o', str(build/binary)], check=True)
        if not args.no_start:
            for label in (UI, SAMPLER):
                stop_job(label, runtime, app)
        data = runtime/'data'
        data.mkdir(mode=0o700, exist_ok=True)
        for name in FILES:
            temporary_file = runtime/(name+'.next')
            shutil.copy2(SOURCE/name, temporary_file)
            temporary_file.replace(runtime/name)
        private_json(config_path, config)
        alert_next = data/'TrafficGuardAlert.next'
        shutil.copy2(build/'TrafficGuardAlert', alert_next)
        alert_next.replace(data/'TrafficGuardAlert')
        if app.exists():
            shutil.rmtree(app)
        executable = app/'Contents/MacOS'
        executable.mkdir(parents=True)
        shutil.copy2(build/'ProxyTraffic', executable/'ProxyTraffic')
        info = dict(CFBundleIdentifier=BUNDLE, CFBundleExecutable='ProxyTraffic',
                    CFBundleName='Proxy Traffic', CFBundleDisplayName='Proxy Traffic',
                    CFBundlePackageType='APPL', CFBundleVersion='1',
                    CFBundleShortVersionString='1.0.0', LSUIElement=True,
                    LSMinimumSystemVersion='13.0', NSHighResolutionCapable=True,
                    NSPrincipalClass='NSApplication', ProxyTrafficRuntime=str(runtime),
                    ProxyTrafficPython=sys.executable, ProxyTrafficTimezone=config['timezone'])
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
    agents.mkdir(parents=True, exist_ok=True)
    jobs = [
        (SAMPLER, launch_agent(SAMPLER, [sys.executable, runtime/'traffic_guard.py', 'run'], runtime, True)),
        (UI, launch_agent(UI, [app/'Contents/MacOS/ProxyTraffic'], runtime, {'SuccessfulExit': False}))]
    for label, job in jobs:
        path = agents/(label+'.plist')
        path.write_bytes(plistlib.dumps(job))
        if not args.no_start:
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    private_json(runtime/'installation.json', dict(
        bundle=BUNDLE, runtime=str(runtime), app=str(app), agents=str(agents),
        python=sys.executable, installed_at=dt.datetime.now(dt.timezone.utc).isoformat()))
    print('Installed app:', app)
    print('Private configuration and history:', runtime)
    print('Daily counters use timezone:', config['timezone'])
    print('Services started.' if not args.no_start else 'Services were not started (--no-start).')


if __name__ == '__main__':
    main()
