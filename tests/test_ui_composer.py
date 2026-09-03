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
        # ── 图与表单共存：两个入口改同一份步骤 ──────────────────
        n0 = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')
        # (a) 图上点箭头加一步
        edges = await pg.eval_on_selector_all(
            '#cgraph g.plus[data-edge]', 'e=>e.map(g=>g.dataset.edge)')
        res['has_graph'] = len(edges) > 0
        # 点线中点的"＋"按钮——它画在节点之后，不会被节点框挡住
        await pg.click('#cgraph g.plus[data-edge="%s"]' % edges[0])
        await pg.wait_for_timeout(250)
        n1 = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')
        res['graph_click_adds_to_shared_list'] = n1 == n0 + 1
        # 图上加的一步，表单侧的步骤列表与 YAML 都要看得见
        y3 = await pg.eval_on_selector('#yaml', 'e=>e.textContent')
        gf, gt = edges[0].split('|')
        res['graph_step_in_yaml'] = (gf in y3 and gt in y3)

        # (b) 再用表单加一步下推，图必须跟着走——选中点应落到新的终点
        await pg.select_option('#stepkind', '下推'); await pg.wait_for_timeout(250)
        o3 = await pg.eval_on_selector_all('#arg1 option', 'e=>e.map(o=>o.value)')
        pick2 = o3[1]
        await pg.select_option('#arg1', pick2); await pg.wait_for_timeout(150)
        await pg.click('#addstep'); await pg.wait_for_timeout(300)
        n2 = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')
        res['form_click_adds_to_shared_list'] = n2 == n1 + 1
        sel_nodes = await pg.eval_on_selector_all(
            '#cgraph [data-sel="1"]', 'e=>e.map(g=>g.dataset.node)')
        res['graph_follows_form'] = sel_nodes == [pick2.split('|')[1]]
        res['selected_nodes'] = sel_nodes
        res['expected_node'] = pick2.split('|')[1]

        # (c) 删一步也要两边同步
        await pg.click('#steps [data-del="0"]'); await pg.wait_for_timeout(250)
        n3 = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')
        res['delete_syncs'] = n3 == n2 - 1

        # ── 起点：不该只有销售订单 ──────────────────────────────
        res['start_options'] = await pg.eval_on_selector_all(
            '#startnouns option', 'e=>e.map(o=>o.value)')
        res['start_chips'] = await pg.eval_on_selector_all(
            '#startchips [data-start]', 'e=>e.map(b=>b.dataset.start)')

        # 换起点：点一个非销售订单的常用起点
        other = next(c for c in res['start_chips'] if c != 'SAL_SaleOrder')
        await pg.click(f'#startchips [data-start="{other}"]')
        await pg.wait_for_timeout(300)
        res['switched_to'] = other
        res['chip_pressed_after_switch'] = await pg.eval_on_selector_all(
            '#startchips [data-start][aria-pressed="true"]', 'e=>e.map(b=>b.dataset.start)')
        res['input_after_switch'] = await pg.eval_on_selector('#opstart', 'e=>e.value')
        res['steps_after_switch'] = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')

        # 撤销要真能把清掉的步骤拿回来
        await pg.click('#startclear'); await pg.wait_for_timeout(300)
        res['steps_after_undo'] = await pg.eval_on_selector_all('#steps .step', 'e=>e.length')

        # 补全：打别名也要认（不是只认中文全名）
        await pg.fill('#opstart', 'PUR_PurchaseOrder')
        await pg.dispatch_event('#opstart', 'change'); await pg.wait_for_timeout(300)
        res['resolved_by_form_id'] = await pg.eval_on_selector_all(
            '#startchips [data-start][aria-pressed="true"]', 'e=>e.map(b=>b.dataset.start)')

        # 认不出来的输入必须说清楚，而不是默默不动
        await pg.fill('#opstart', '这不是个单据')
        await pg.dispatch_event('#opstart', 'change'); await pg.wait_for_timeout(300)
        res['bad_input_invalid'] = await pg.eval_on_selector(
            '#opstart', 'e=>e.getAttribute("aria-invalid")')
        res['bad_input_hint'] = await pg.eval_on_selector('#starthint', 'e=>e.textContent')

        # 起点栏在加了一步之后不能开始说谎
        await pg.click(f'#startchips [data-start="{other}"]')
        await pg.wait_for_timeout(250)
        plus = await pg.eval_on_selector_all(
            '#cgraph g.plus[data-edge]', 'e=>e.map(g=>g.dataset.edge)')
        edge = next((p for p in plus if p.startswith(other + '|')), None)
        if edge:
            await pg.click(f'#cgraph g.plus[data-edge="{edge}"]')
            await pg.wait_for_timeout(300)
            res['start_after_adding_step'] = await pg.eval_on_selector_all(
                '#startchips [data-start][aria-pressed="true"]', 'e=>e.map(b=>b.dataset.start)')
            res['edge_used'] = edge

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
        from kingdee_ontology.base.ontology import load as _load
        _load.cache_clear()
        o = _load(tenant="example-tenant")
        verb, offered = ui["verb"], ui["objects_for_verb"]
        bad = [f for f in offered if verb not in o.nouns[f].allowed_verbs]
        assert not bad, f"动词 {verb} 的对象下拉里混进了不适用的类型：{bad}"
        _load.cache_clear()

    def test_typing_elsewhere_does_not_clobber_selection(self, ui):
        """在别处打字不该冲掉控件区的选择——这正是原 bug 的触发方式。"""
        assert ui["survives_text_input"] is True


class TestGraphAndFormCoexist:
    """图和表单不是二选一，是同一份步骤的两个入口。

    图上点适合顺着下推链走；确认、动词、守卫这些链上没有的步骤只能从
    表单补。所以两边必须改同一个列表、并且互相看得见对方的改动——
    否则就是同一份文档开了两个编辑器，各说各话。
    """

    def test_graph_is_rendered(self, ui):
        assert ui["has_graph"] is True, "编排页没有链路图，节点入口根本不存在"

    def test_graph_click_feeds_the_shared_step_list(self, ui):
        assert ui["graph_click_adds_to_shared_list"] is True, (
            "在图上点箭头没有加进表单侧的步骤列表——两边不是同一份 steps")

    def test_graph_step_reaches_the_same_yaml(self, ui):
        assert ui["graph_step_in_yaml"] is True, "图上加的一步没有进 YAML"

    def test_form_click_feeds_the_shared_step_list(self, ui):
        assert ui["form_click_adds_to_shared_list"] is True, (
            "表单加的一步没进共用列表")

    def test_graph_follows_steps_added_from_the_form(self, ui):
        """表单加了一步下推，图上的选中点要跟着走到新终点。

        这是共存的关键：早先只有图 → 表单单向同步，从表单加完步骤后图
        还停在原处，接着在图上点就会从错误的起点接下去。
        """
        assert ui["graph_follows_form"] is True, (
            f"表单加完步骤后图没跟上：选中 {ui['selected_nodes']}，"
            f"应为 [{ui['expected_node']!r}]")

    def test_delete_syncs_both_entrances(self, ui):
        assert ui["delete_syncs"] is True, "删步骤后两边没同步"

    def test_generated_yaml_is_valid_profile(self, ui, tmp_path, monkeypatch):
        """编排器产出的 YAML 必须真能过校验器，不是"看着像"。"""
        import yaml as _yaml
        doc = _yaml.safe_load(ui["yaml"])
        assert "operations" in doc and doc["operations"]
        sys.path.insert(0, str(ROOT))
        from kingdee_ontology.base import ontology as ont
        import kingdee_ontology.base.validate_profile as vp
        prof = {"tenant": "_ui", **doc}
        monkeypatch.setattr(ont, "load_profile", lambda t: prof if t == "_ui" else None)
        monkeypatch.setattr(vp, "load_profile", lambda t: prof if t == "_ui" else None)
        ont.load.cache_clear()
        errs, _ = vp.validate("_ui")
        ont.load.cache_clear()
        assert errs == [], f"编排器产出的配置过不了校验：{errs}"


class TestEntryPointIsNotOnlySalesOrder:
    """起点不该只有销售订单。

    后端从来没这个限制——示例租户里「采购收货入库」就起于采购订单。是界面把
    一个**默认值**演成了约束：预置步骤硬编码一条 SAL_SaleOrder 的下推，而页面上
    没有任何地方能换。默认值被当成约束，是界面在替本体说话，而且说错了。
    """

    def test_many_entry_points_are_offered(self, ui):
        assert len(ui["start_options"]) > 20, (
            f"可选起点只有 {len(ui['start_options'])} 个，补全列表没建起来")

    def test_common_entry_points_include_more_than_sales(self, ui):
        chips = ui["start_chips"]
        assert len(chips) >= 3, f"常用起点太少：{chips}"
        assert chips != ["SAL_SaleOrder"], "常用起点只有销售订单，等于没改"

    def test_switching_entry_point_takes_effect(self, ui):
        """点一个别的起点，输入框、chip、步骤都得跟着变——这正是原来做不到的事。"""
        assert ui["chip_pressed_after_switch"] == [ui["switched_to"]], (
            f"切到 {ui['switched_to']} 后高亮的却是 {ui['chip_pressed_after_switch']}")
        assert ui["input_after_switch"], "切换后输入框没回填名称"
        assert ui["steps_after_switch"] == 0, "换起点应清空原有步骤（否则两条链混在一起）"

    def test_undo_restores_the_cleared_steps(self, ui):
        """换起点会清空步骤，那就必须能撤销——静默丢掉别人的活是不可接受的。"""
        assert ui["steps_after_undo"] > 0, "撤销没把清掉的步骤拿回来"

    def test_form_id_also_resolves(self, ui):
        """补全要认 form_id，不只认中文名。"""
        assert ui["resolved_by_form_id"] == ["PUR_PurchaseOrder"], (
            f"输入 form_id 没解析出来：{ui['resolved_by_form_id']}")

    def test_unrecognized_input_says_so(self, ui):
        """认不出来要说清楚。「点了没反应」比报错更难查。"""
        assert ui["bad_input_invalid"] == "true", "无效输入没有标 aria-invalid"
        assert "认不出" in ui["bad_input_hint"], f"没给出提示：{ui['bad_input_hint']}"

    def test_start_does_not_drift_when_steps_are_added(self, ui):
        """加了一步之后，起点仍是起点。

        cSel 是「当前位置」，commitSteps() 会把它推到步骤链末端。起点栏若跟着
        cSel 走，加一步就会显示成下游单——这是把两个概念混用的必然结果。
        """
        if "start_after_adding_step" not in ui:
            pytest.skip("该起点没有可下推的边，测不了漂移")
        assert ui["start_after_adding_step"] == [ui["switched_to"]], (
            f"加了一步 {ui['edge_used']} 之后，起点漂成了 "
            f"{ui['start_after_adding_step']}，应仍是 {ui['switched_to']}")
