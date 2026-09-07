"""Run isolated browser review; no real database or remote judge is used."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PLAYWRIGHT = os.environ.get('REVIEW_PLAYWRIGHT_MODULE', '/home/ubuntu/.npm/_npx/b234c773f454f454/node_modules/playwright')
CHROMIUM = os.environ.get('REVIEW_CHROMIUM_EXE', '/home/ubuntu/.cache/ms-playwright/chromium-1234/chrome-linux/chrome')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='frontend-system-review-') as directory:
        root = Path(directory)
        api_port, judge_port = free_port(), free_port()
        api_base = f'http://127.0.0.1:{api_port}'
        replacements = {
            '__REVIEW_ROOT__': str(root), '__REPO_ROOT__': str(REPO),
            '__API_BASE__': api_base, '__JUDGE_BASE__': f'http://127.0.0.1:{judge_port}',
            '__JUDGE_PORT__': str(judge_port), '__PLAYWRIGHT_MODULE__': PLAYWRIGHT,
            '__CHROMIUM_EXE__': CHROMIUM,
        }
        for template in (HERE / 'templates').iterdir():
            content = template.read_text()
            for key, value in replacements.items():
                content = content.replace(key, value)
            (root / template.name).write_text(content)
        env = {**os.environ,
               'TRANSLATION_EVAL_DATABASE_URL': f'sqlite:///{root / "eval.db"}',
               'TRANSLATION_EVAL_IMPORT_DIR': str(root / 'imports'),
               'TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE': str(root / 'heartbeat')}
        processes = []
        with (root / 'services.log').open('w') as log:
            try:
                processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(api_port)], cwd=REPO, env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen([sys.executable, str(root / 'judge.py')], cwd=REPO, env=env, stdout=log, stderr=log))
                for _ in range(100):
                    try:
                        if httpx.get(api_base + '/api/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Isolated API startup failed')

                def browser(name: str) -> None:
                    subprocess.run(['node', str(root / name)], cwd=REPO, check=True, env=env)

                def worker() -> None:
                    subprocess.run([sys.executable, '-m', 'app.worker', '--once'], cwd=REPO, check=True, env=env)

                browser('browser.cjs')
                dataset = json.loads((root / 'dataset.json').read_text())[0]
                for name, bad in [('valid-results', False), ('bad-results', True)]:
                    fixture = root / name
                    shutil.copytree(REPO / 'examples/results/demo-run', fixture)
                    manifest_path = fixture / 'result_info.json'
                    manifest = json.loads(manifest_path.read_text())
                    manifest['datasets'][0]['dataset_content_sha256'] = dataset['latest_version']['content_sha256']
                    manifest['inference']['mode'] = 'default'
                    if bad:
                        manifest['datasets'][0]['dataset_key'] = 'wrong-key'
                    manifest_path.write_text(json.dumps(manifest))
                browser('submission.cjs')
                browser('bad-manifest.cjs')
                browser('cancel-error.cjs')
                worker()
                browser('results.cjs')
                worker()
                browser('settings.cjs')
                worker()
                worker()
                browser('compare.cjs')
                task = httpx.get(api_base + '/api/tasks').json()[0]
                assert task['status'] == 'completed'
                assert task['force_reevaluate'] is True
                jobs = task['dataset_jobs'][0]['evaluator_jobs']
                assert len(jobs) == 2 and all(job['completed_items'] == 6 and job['cached_items'] == 0 for job in jobs)
                assert len((root / 'judge-requests.jsonl').read_text().splitlines()) == 6
                print('Browser review completed: BLEU + local mock LLM each scored 6 items; isolated services and data will be removed.')
                if os.environ.get('REVIEW_OUTPUT_DIR'):
                    output = Path(os.environ['REVIEW_OUTPUT_DIR']).resolve()
                    output.mkdir(parents=True, exist_ok=True)
                    for path in root.glob('evidence-*.json'):
                        shutil.copy2(path, output / path.name)
                    for path in root.glob('*.png'):
                        shutil.copy2(path, output / path.name)
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == '__main__':
    main()
