from pathlib import Path


def test_main_spec_preserves_packaged_brand_assets():
    spec_text = (Path(__file__).resolve().parents[1] / "main.spec").read_text(
        encoding="utf-8"
    )

    assert "('assets', 'assets')" in spec_text
    assert "icon='assets/logo.ico'" in spec_text
    assert "name='智能电机检测'" in spec_text
