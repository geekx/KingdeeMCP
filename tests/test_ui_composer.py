"""编排器的下拉必须真的生效（回归）。

曾有一个 bug：renderCompose 绑在整个面板的 input 事件上，而它会重建
#argwrap 的 innerHTML。用户一选下拉就触发重建，select 被换成新元素、
回到第一项——**永远只有第一条能进 YAML**，选什么都没用。

这类"选了不生效"的缺陷靠读代码很难发现（每个函数单看都对），
必须真的驱动浏览器点一遍。所以这组测试跑真 Chromium。
"""
import glob
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "docs" / "ontology" / "ui" / "explorer.html"

pytest.importorskip("playwright", reason="需要 playwright 才能驱动真实浏览器")
_CHROME = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
pytestmark = pytest.mark.skipif(
    not _CHROME or not UI.exists(),
    reason="需要预装的 Chromium 与已生成的 explorer.html")

DRIVER = r'''
import asyncio, json, sys
from playwright.async_api import async_playwright

UI, EXE, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
SK = ('<!doctype html><html><head><meta charset="utf-8">'
      '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>')
open('/tmp/_ui_test.html', 'w', encoding='utf-8').write(
    SK + open(UI, encoding='utf-8').read() + "</body></html>")

async def main():
    res = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE)
        pg = await b.new_page(viewport={'width': 1440, 'height': 900})
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto('file:///tmp/_ui_test.html')
        await pg.wait_for_timeout(1200)
        await pg.click('[data-view="compose"]')
        await pg.wait_for_timeout(300)

        opts = await pg.eval_on_selector_all('#arg1 option', 'e=>e.map(o=>o.value)')
        res['link_options'] = len(opts)
        pick = opts[2]
        await pg.select_option('#arg1', pick)
        await pg.wait_for_timeout(200)
        res['kept_selection'] = (await pg.eval_on_selector('#arg1', 'e=>e.value')) == pick
        await pg.click('#addstep'); await pg.wait_for_timeout(200)
        y = await pg.eval_on_selector('#yaml', 'e=>e.textContent')
        f, t = pick.split('|')
        res['push_step_in_yaml'] = (f in y.splitlines()[-1] and t in y.splitlines()[-1])
        res['picked'] = pick

        await pg.select_option('#stepkind', 'verb'); await pg.wait_for_timeout(250)
        vs = await pg.eval_on_selector_all('#arg1 option', 'e=>e.map(o=>o.value)')
        await pg.select_option('#arg1', vs[3]); await pg.wait_for_timeout(250)
        # 对象下拉必须已按所选动词过滤过
        ns = await pg.eval_on_selector_all('#arg2 option', 'e=>e.map(o=>o.value)')
        res['verb'] = vs[3]
        res['objects_for_verb'] = ns
        await pg.select_option('#arg2', ns[min(5, len(ns) - 1)]); await pg.wait_for_timeout(150)
        res['picked_obj'] = ns[min(5, len(ns) - 1)]
        await pg.select_option('#arg3', 'targets'); await pg.wait_for_timeout(150)
        await pg.click('#addstep'); await pg.wait_for_timeout(200)
        y2 = (await pg.eval_on_selector('#yaml', 'e=>e.textContent')).splitlines()[-1]
        res['verb_step_line'] = y2.strip()
        res['verb_step_ok'] = (vs[3] in y2 and res['picked_obj'] in y2 and 'targets' in y2)

        # 改操作名后，控件选择不该被冲掉
        await pg.select_option('#stepkind', '下推'); await pg.wait_for_timeout(250)
        o2 = await pg.eval_on_selector_all('#arg1 option', 'e=>e.map(o=>o.value)')
        await pg.select_option('#arg1', o2[4]); await pg.wait_for_timeout(150)
        await pg.fill('#opname', '采购收货'); await pg.wait_for_timeout(250)
        res['survives_text_input'] = (await pg.eval_on_selector('#arg1', 'e=>e.value')) == o2[4]
        res['yaml'] = await pg.eval_on_selector('#yaml', 'e=>e.textContent')
        res['errors'] = [e for e in errs if 'ERR_CONNECTION' not in e]
        await b.close()
    open(OUT, 'w', encoding='utf-8').write(json.dumps(res, ensure_ascii=False))

asyncio.run(main())
'''


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    d = tmp_path_factory.mktemp("ui")
    drv, out = d / "drv.py", d / "out.json"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([sys.executable, str(drv), str(UI), _CHROME[0], str(out)],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        pytest.fail(f"浏览器驱动失败：{r.stderr[-1500:]}")
    import json
    return json.loads(out.read_text(encoding="utf-8"))


class TestComposerDropdowns:
    def test_no_js_errors(self, ui):
        assert ui["errors"] == []

    def test_selection_is_not_reset(self, ui):
        """选中项必须保住——曾因重建 innerHTML 而被冲回第一项。"""
        assert ui["kept_selection"] is True, (
            "选完下拉后 select 的值变了，说明控件又被重建了")

    def test_selected_link_reaches_the_yaml(self, ui):
        assert ui["push_step_in_yaml"] is True, (
            f"选了 {ui['picked']}，但它没进 YAML —— 又只有第一条生效了")

    def test_all_three_verb_dropdowns_take_effect(self, ui):
        assert ui["verb_step_ok"] is True, (
            f"动词步的三个下拉没有全部生效：{ui['verb_step_line']}")

    def test_object_dropdown_is_filtered_by_verb(self, ui):
        """界面不该让人拼出校验器会拒的组合。

        原来对象下拉列出所有有动作的类型，于是能选出「反审核 + 仓库」——
        基础资料没有审核流。让人拼出一个不成立的东西，比不让他拼更糟：
        他会以为是配置系统坏了。
        """
        sys.path.insert(0, str(ROOT))
        from base.ontology import load as _load
        _load.cache_clear()
        o = _load(tenant="example-tenant")
        verb, offered = ui["verb"], ui["objects_for_verb"]
        bad = [f for f in offered if verb not in o.nouns[f].allowed_verbs]
        assert not bad, f"动词 {verb} 的对象下拉里混进了不适用的类型：{bad}"
        _load.cache_clear()

    def test_typing_elsewhere_does_not_clobber_selection(self, ui):
        """在别处打字不该冲掉控件区的选择——这正是原 bug 的触发方式。"""
        assert ui["survives_text_input"] is True

    def test_generated_yaml_is_valid_profile(self, ui, tmp_path, monkeypatch):
        """编排器产出的 YAML 必须真能过校验器，不是"看着像"。"""
        import yaml as _yaml
        doc = _yaml.safe_load(ui["yaml"])
        assert "operations" in doc and doc["operations"]
        sys.path.insert(0, str(ROOT))
        from base import ontology as ont
        import base.validate_profile as vp
        prof = {"tenant": "_ui", **doc}
        monkeypatch.setattr(ont, "load_profile", lambda t: prof if t == "_ui" else None)
        monkeypatch.setattr(vp, "load_profile", lambda t: prof if t == "_ui" else None)
        ont.load.cache_clear()
        errs, _ = vp.validate("_ui")
        ont.load.cache_clear()
        assert errs == [], f"编排器产出的配置过不了校验：{errs}"
