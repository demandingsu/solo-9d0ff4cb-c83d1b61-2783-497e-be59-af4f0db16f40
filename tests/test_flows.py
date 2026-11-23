"""端到端自测:批次工作流、核对/发布、联动换版、交错提交、失效规则。"""

# ---------------------------------------------------------------- 工具


def make_batch(client, ops, title=""):
    r = client.post("/batches", json={"title": title, "ops": ops})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def verify(client, bid):
    r = client.post(f"/batches/{bid}/verify")
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, bid):
    return client.post(f"/batches/{bid}/publish")


def publish_ok(client, bid):
    r = publish(client, bid)
    assert r.status_code == 200, r.text
    return r.json()


def upsert_item(code, nominal, upper, lower, name="检验项目"):
    return {
        "op": "upsert_item",
        "item_code": code,
        "name": name,
        "nominal_um": nominal,
        "upper_um": upper,
        "lower_um": lower,
    }


def upsert_ws(code, gen, name="工位"):
    return {"op": "upsert_workstation", "workstation_code": code, "name": name, "terminal_generation": gen}


def claim(ws, item):
    return {"op": "claim_item", "workstation_code": ws, "item_code": item}


def unclaim(ws, item):
    return {"op": "unclaim_item", "workstation_code": ws, "item_code": item}


ITEM_SYM = upsert_item("ITEM-SYM", 10000, 10005, 9995, "对称项目")      # 公差 ±5
ITEM_ASYM = upsert_item("ITEM-ASYM", 20000, 20008, 19996, "非对称项目")  # +8/-4
WS_NEW = upsert_ws("WS-NEW", "new", "新终端工位")
WS_OLD = upsert_ws("WS-OLD", "old", "旧终端工位")


def setup_base(client):
    """基础数据:两个项目(对称/非对称)+ 新旧两个工位;旧工位领用对称项目。"""
    bid = make_batch(
        client,
        [
            ITEM_SYM,
            ITEM_ASYM,
            WS_NEW,
            WS_OLD,
            claim("WS-NEW", "ITEM-SYM"),
            claim("WS-NEW", "ITEM-ASYM"),
            claim("WS-OLD", "ITEM-SYM"),
        ],
        title="基础数据",
    )
    res = verify(client, bid)
    assert res["status"] == "verified"
    publish_ok(client, bid)


# ---------------------------------------------------------------- 基本流程


def test_happy_path_and_spec_formats(client):
    setup_base(client)

    new_spec = client.get("/workstations/WS-NEW/spec").json()
    assert new_spec["terminal_generation"] == "new"
    by_code = {i["item_code"]: i for i in new_spec["items"]}
    assert by_code["ITEM-ASYM"] == {
        "item_code": "ITEM-ASYM", "name": "非对称项目",
        "nominal_um": 20000, "upper_um": 20008, "lower_um": 19996,
    }

    old_spec = client.get("/workstations/WS-OLD/spec").json()
    assert old_spec["terminal_generation"] == "old"
    assert old_spec["items"] == [
        {"item_code": "ITEM-SYM", "name": "对称项目", "nominal_um": 10000, "tolerance_um": 5}
    ]
    assert "upper_um" not in old_spec["items"][0]

    # 每次返回一整份;内容不变则 revision 不变
    again = client.get("/workstations/WS-OLD/spec").json()
    assert again["revision"] == old_spec["revision"]
    assert again["items"] == old_spec["items"]


def test_data_not_effective_before_publish(client):
    bid = make_batch(client, [ITEM_SYM, WS_NEW, claim("WS-NEW", "ITEM-SYM")])
    assert client.get("/items").json() == []
    assert client.get("/workstations/WS-NEW/spec").status_code == 404
    verify(client, bid)
    assert client.get("/items").json() == []  # 核对不生效
    publish_ok(client, bid)
    assert [i["code"] for i in client.get("/items").json()] == ["ITEM-SYM"]
    assert client.get("/workstations/WS-NEW/spec").status_code == 200


def test_old_terminal_asymmetric_rejected(client):
    """旧终端领用非对称公差 → 核对列出工位和项目,整批退回。"""
    setup_base(client)
    bid = make_batch(client, [claim("WS-OLD", "ITEM-ASYM")])
    res = verify(client, bid)
    assert res["status"] == "rejected"
    assert [(v["workstation_code"], v["item_code"]) for v in res["violations"]] == [
        ("WS-OLD", "ITEM-ASYM")
    ]
    # 被退回的批次不能发布
    r = publish(client, bid)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "batch_not_verified"


def test_fix_after_reject_then_publish(client):
    """退回后把项目改成对称公差,重新核对发布,旧工位按公差格式取到。"""
    setup_base(client)
    bid = make_batch(client, [claim("WS-OLD", "ITEM-ASYM")])
    assert verify(client, bid)["status"] == "rejected"

    r = client.put(
        f"/batches/{bid}/ops",
        json={"ops": [
            upsert_item("ITEM-ASYM", 20000, 20008, 19992, "非对称项目"),  # 改为 ±8
            claim("WS-OLD", "ITEM-ASYM"),
        ]},
    )
    assert r.status_code == 200 and r.json()["status"] == "draft"
    assert verify(client, bid)["status"] == "verified"
    publish_ok(client, bid)

    old_spec = client.get("/workstations/WS-OLD/spec").json()
    by_code = {i["item_code"]: i for i in old_spec["items"]}
    assert by_code["ITEM-ASYM"]["tolerance_um"] == 8


def test_terminal_flip_to_old_with_asymmetric_claim_rejected(client):
    """把领用了非对称项目的工位改成旧终端 → 核对退回。"""
    setup_base(client)
    bid = make_batch(client, [upsert_ws("WS-NEW", "old", "新终端工位")])
    res = verify(client, bid)
    assert res["status"] == "rejected"
    assert ("WS-NEW", "ITEM-ASYM") in [
        (v["workstation_code"], v["item_code"]) for v in res["violations"]
    ]


def test_verify_preview_lists_relevant_workstations(client):
    """核对时给出相关工位改完后将领用的规范预览。"""
    setup_base(client)
    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    res = verify(client, bid)
    assert res["status"] == "verified"
    ws_codes = {p["workstation_code"] for p in res["preview"]}
    assert ws_codes == {"WS-NEW", "WS-OLD"}  # 两个工位都领用了 ITEM-SYM
    for p in res["preview"]:
        item = next(i for i in p["items"] if i["item_code"] == "ITEM-SYM")
        assert (item["upper_um"], item["lower_um"]) == (10006, 9994)


# ---------------------------------------------------------------- 联动换版


def test_linked_version_switch(client):
    """发布后规范版本 +1,按工位取到的整份规范同步换版。"""
    setup_base(client)
    before = client.get("/workstations/WS-NEW/spec").json()
    assert client.get("/items/ITEM-SYM").json()["version"] == 1

    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    verify(client, bid)
    out = publish_ok(client, bid)
    assert out["changes"] == [{"kind": "update_item", "code": "ITEM-SYM", "version": 2}]

    item = client.get("/items/ITEM-SYM").json()
    assert item["version"] == 2 and item["upper_um"] == 10006

    new_spec = client.get("/workstations/WS-NEW/spec").json()
    assert new_spec["revision"] != before["revision"]
    sym = next(i for i in new_spec["items"] if i["item_code"] == "ITEM-SYM")
    assert (sym["upper_um"], sym["lower_um"]) == (10006, 9994)

    old_spec = client.get("/workstations/WS-OLD/spec").json()
    assert old_spec["items"][0]["tolerance_um"] == 6


def test_claim_change_bumps_claims_version(client):
    setup_base(client)
    before = client.get("/workstations/WS-NEW").json()
    item_before = client.get("/items/ITEM-ASYM").json()
    bid = make_batch(client, [unclaim("WS-NEW", "ITEM-ASYM")])
    verify(client, bid)
    publish_ok(client, bid)
    after = client.get("/workstations/WS-NEW").json()
    assert after["claims_version"] == before["claims_version"] + 1
    assert after["version"] == before["version"]  # 工位配置本身没变
    assert after["claimed_items"] == ["ITEM-SYM"]
    item = client.get("/items/ITEM-ASYM").json()
    assert item["claims_version"] == item_before["claims_version"] + 1
    assert item["version"] == item_before["version"]  # 规范值没变


# ---------------------------------------------------------------- 交错提交


def test_interleaved_spec_change_requires_reverify(client):
    """A 核对后,B 改了同一项目并发布 → A 发布被拒,需重新核对后再发布。"""
    setup_base(client)
    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    bid_b = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10007, 9993, "对称项目")], "B")
    assert verify(client, bid_a)["status"] == "verified"
    assert verify(client, bid_b)["status"] == "verified"
    publish_ok(client, bid_b)

    r = publish(client, bid_a)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "verification_stale"
    assert any("ITEM-SYM" in c for c in detail["changed_entities"])
    assert client.get(f"/batches/{bid_a}").json()["status"] == "draft"

    assert verify(client, bid_a)["status"] == "verified"
    publish_ok(client, bid_a)
    item = client.get("/items/ITEM-SYM").json()
    assert (item["upper_um"], item["lower_um"], item["version"]) == (10006, 9994, 3)


def test_claim_change_invalidates_even_if_reverted(client):
    """核对后有人增删过待改项目的领用,即使改回原样也要重新核对。"""
    setup_base(client)
    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    assert verify(client, bid_a)["status"] == "verified"

    bid_b = make_batch(client, [unclaim("WS-NEW", "ITEM-SYM")], "B-取消领用")
    verify(client, bid_b)
    publish_ok(client, bid_b)
    bid_c = make_batch(client, [claim("WS-NEW", "ITEM-SYM")], "C-恢复领用")
    verify(client, bid_c)
    publish_ok(client, bid_c)

    # 领用关系与核对时完全相同,但版本计数器已变 → 仍须重新核对
    r = publish(client, bid_a)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "verification_stale"

    assert verify(client, bid_a)["status"] == "verified"
    publish_ok(client, bid_a)


def test_workstation_config_change_invalidates(client):
    """核对后所涉工位配置变过 → 重新核对。"""
    setup_base(client)
    bid_a = make_batch(client, [upsert_ws("WS-NEW", "new", "改名后的工位")], "A")
    assert verify(client, bid_a)["status"] == "verified"
    bid_b = make_batch(client, [upsert_ws("WS-NEW", "new", "另一个名字")], "B")
    verify(client, bid_b)
    publish_ok(client, bid_b)

    r = publish(client, bid_a)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "verification_stale"


def test_unrelated_change_does_not_block(client):
    """无关改动(其他项目、其他工位)不影响发布。"""
    setup_base(client)
    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    assert verify(client, bid_a)["status"] == "verified"

    bid_b = make_batch(
        client,
        [upsert_item("ITEM-OTHER", 5000, 5002, 4998, "无关项目"), upsert_ws("WS-X", "new", "无关工位")],
        "B-无关",
    )
    verify(client, bid_b)
    publish_ok(client, bid_b)

    publish_ok(client, bid_a)  # 不受影响
    assert client.get("/items/ITEM-SYM").json()["upper_um"] == 10006


def test_publish_safety_net(client):
    """交错场景:核对时合法、发布时才出现的违规,发布安全网兜底退回。"""
    setup_base(client)
    # 新工位 WS-X 领用对称项目 ITEM-FLEX
    bid0 = make_batch(
        client,
        [upsert_item("ITEM-FLEX", 30000, 30005, 29995, "可变项目"),
         upsert_ws("WS-X", "new", "试验工位"),
         claim("WS-X", "ITEM-FLEX")],
    )
    verify(client, bid0)
    publish_ok(client, bid0)

    # A:把 WS-X 翻成旧终端 —— 核对时 ITEM-FLEX 仍对称,通过
    bid_a = make_batch(client, [upsert_ws("WS-X", "old", "试验工位")], "A-换代")
    assert verify(client, bid_a)["status"] == "verified"
    # B:把 ITEM-FLEX 改成非对称 —— 此时 WS-X 还是新终端,B 合法发布
    bid_b = make_batch(client, [upsert_item("ITEM-FLEX", 30000, 30008, 29996, "可变项目")], "B")
    verify(client, bid_b)
    publish_ok(client, bid_b)

    # A 的指纹未涉及 ITEM-FLEX,指纹仍新鲜,但发布安全网必须拦住
    r = publish(client, bid_a)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "asymmetric_tolerance_on_old_terminal"
    assert [(v["workstation_code"], v["item_code"]) for v in detail["violations"]] == [
        ("WS-X", "ITEM-FLEX")
    ]
    assert client.get(f"/batches/{bid_a}").json()["status"] == "rejected"
    # 生效状态未被破坏
    assert client.get("/workstations/WS-X").json()["terminal_generation"] == "new"


# ---------------------------------------------------------------- 其他规则


def test_ops_replace_resets_verification(client):
    setup_base(client)
    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    assert verify(client, bid)["status"] == "verified"
    r = client.put(f"/batches/{bid}/ops", json={"ops": [upsert_item("ITEM-SYM", 10000, 10007, 9993, "对称项目")]})
    assert r.json()["status"] == "draft"
    assert publish(client, bid).status_code == 409  # 未重新核对不能发


def test_published_batch_immutable(client):
    setup_base(client)
    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    verify(client, bid)
    publish_ok(client, bid)
    assert client.put(f"/batches/{bid}/ops", json={"ops": []}).status_code == 409
    assert client.delete(f"/batches/{bid}").status_code == 409
    assert publish(client, bid).status_code == 409


def test_validation_and_op_errors(client):
    # 上下限必须包住标称值
    r = client.post("/batches", json={"ops": [{**upsert_item("X", 100, 99, 98)}]})
    assert r.status_code == 422
    # 领用不存在的项目 → 核对报操作错误
    setup_base(client)
    bid = make_batch(client, [claim("WS-NEW", "ITEM-MISSING")])
    r = client.post(f"/batches/{bid}/verify")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "op_error"


def test_query_endpoints(client):
    setup_base(client)
    items = {i["code"]: i for i in client.get("/items").json()}
    assert items["ITEM-SYM"]["symmetric"] is True
    assert items["ITEM-ASYM"]["symmetric"] is False
    detail = client.get("/items/ITEM-SYM").json()
    assert detail["claimed_by"] == ["WS-NEW", "WS-OLD"]
    ws = client.get("/workstations/WS-NEW").json()
    assert ws["claimed_items"] == ["ITEM-ASYM", "ITEM-SYM"]
    assert client.get("/items/NOPE").status_code == 404
    assert client.get("/workstations/NOPE").status_code == 404
    assert client.get("/workstations/NOPE/spec").status_code == 404
