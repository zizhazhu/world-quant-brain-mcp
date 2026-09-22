"""Image-only smoke checks; no BRAIN credentials or external network required."""

import asyncio
from pathlib import Path
import sys
import time

import requests


async def check_image():
    root = Path('/app')
    forbidden_dirs = {'.git', '.github', '.venv', 'venv', 'env', 'cache',
                      'downloads', 'results', 'node_modules'}
    for path in root.rglob('*'):
        name = path.name
        assert name not in forbidden_dirs, f'Unexpected build content: {path}'
        assert not (name.startswith('.env') or name in {
            'user_config.json', '.brain_mcp_config.json', '.candidate_pool.json.lock'
        } or name.startswith('candidate_pool.json') or path.suffix in {'.pem', '.key'}), (
            f'Unexpected credential or runtime file: {path}'
        )
    for name in ('main.py', 'requirements.txt', 'config/info_data.bin'):
        assert (root / name).is_file(), f'Missing runtime file: {name}'

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=['--no-sandbox']
        )
        try:
            page = await browser.new_page()
            await page.goto('about:blank')
            assert page.url == 'about:blank'
        finally:
            await browser.close()
    print('Image contents and Chromium checks passed.')


async def check_runtime():
    deadline = time.monotonic() + 120
    session = requests.Session()
    session.trust_env = False
    while True:
        try:
            response = session.get('http://127.0.0.1:8000/health', timeout=2)
            response.raise_for_status()
            assert response.json()['status'] == 'healthy'
            break
        except (requests.RequestException, ValueError, KeyError, AssertionError):
            if time.monotonic() >= deadline:
                raise RuntimeError('MCP did not become healthy within 120 seconds')
            await asyncio.sleep(1)

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def check_protocol():
        async with streamable_http_client('http://127.0.0.1:8000/mcp') as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert tools.tools, 'MCP returned no tools'
                print(f'MCP initialized and returned {len(tools.tools)} tools.')

    await asyncio.wait_for(check_protocol(), timeout=20)


if __name__ == '__main__':
    if sys.argv[1:] == ['image']:
        asyncio.run(check_image())
    elif sys.argv[1:] == ['runtime']:
        asyncio.run(check_runtime())
    else:
        raise SystemExit('Usage: ci_image_smoke.py image|runtime')
