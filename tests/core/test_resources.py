from k8ostester.core.resources import load_resource

def test_load_resource(tmp_path):
    tpl = tmp_path / "template.yaml"
    tpl.write_text("name: ${NAME}\nvalue: ${VALUE}")
    res = load_resource(tpl, {"NAME": "foo", "VALUE": "bar"})
    assert res == {"name": "foo", "value": "bar"}
