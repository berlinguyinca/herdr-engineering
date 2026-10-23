#!/usr/bin/env python3
"""Explicitly provision the optional private CPU model for agent summaries."""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MODEL_FILE = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
MODEL_SHA256 = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
MODEL_URL = ('https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/'
             '91cad51170dc346986eccefdc2dd33a9da36ead9/' + MODEL_FILE)
IMAGE = ('ghcr.io/ggml-org/llama.cpp:server@sha256:'
         '8c74db29f1a2bef3f0a331b52c4d6214601dfceed4bee0b94cc48b3b23ff257d')
COMPOSE = Path(__file__).resolve().with_name('compose.summary.yaml')


def verified(path):
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == MODEL_SHA256


def prepare_model(directory):
    """Reuse a verified model; otherwise publish only a complete verified download."""
    target = directory / MODEL_FILE
    if verified(target):
        return target
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.qwen-', delete=False) as dest:
            temp_path = Path(dest.name)
            with urllib.request.urlopen(MODEL_URL, timeout=60) as source:
                while block := source.read(1024 * 1024):
                    dest.write(block)
            dest.flush()
            os.fsync(dest.fileno())
        if not verified(temp_path):
            raise ValueError('model checksum mismatch; download discarded')
        temp_path.replace(target)
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'up', 'status', 'down'])
    parser.add_argument('--model-dir', type=Path,
                        default=Path.home() / '.cache/herdr-engineering/models')
    parser.add_argument('--project', default='herdr-control-plane',
                        help='existing control-plane Compose project whose bridge to join')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.project):
        parser.error('--project must be a lowercase Compose project name')
    directory = args.model_dir.expanduser().resolve()
    env = dict(os.environ, HERDR_SUMMARY_MODEL_DIR=str(directory),
               HERDR_SUMMARY_NETWORK=args.project + '_default')
    # Separate project means `down` cannot stop the control-plane application.
    compose = ['docker', 'compose', '-p', args.project + '-summaries', '-f', str(COMPOSE)]
    try:
        if args.action == 'prepare':
            prepare_model(directory)
            subprocess.run(['docker', 'pull', IMAGE], check=True)
            print('Verified model and pinned runtime are ready; run up to start the service.')
        elif args.action == 'up':
            if not verified(directory / MODEL_FILE):
                raise ValueError('verified model unavailable; run prepare explicitly first')
            subprocess.run(['docker', 'image', 'inspect', IMAGE], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(['docker', 'network', 'inspect', args.project + '_default'],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(compose + ['up', '-d', '--no-build', '--pull', 'never', '--wait',
                                      '--wait-timeout', '120', 'agent-summary-model'],
                           env=env, check=True)
            print('Summary model healthy at http://agent-summary-model:8080/v1 on the private bridge.')
        elif args.action == 'status':
            subprocess.run(compose + ['ps'], env=env, check=True)
        else:
            subprocess.run(compose + ['down'], env=env, check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Summary model setup failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
