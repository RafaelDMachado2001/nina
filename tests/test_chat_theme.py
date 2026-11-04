import unittest

from nina import chat_theme as th


class ThemeTest(unittest.TestCase):
    def test_every_text_pair_reaches_wcag_aa_in_both_themes(self):
        for name, t in th.THEMES.items():
            for fg, bg in th.TEXT_PAIRS:
                self.assertGreaterEqual(th.contrast(t[fg], t[bg]), 4.5, f"{name}: {fg} on {bg}")

    def test_both_themes_define_the_same_tokens(self):
        self.assertEqual(set(th.THEMES["dark"]), set(th.THEMES["light"]))

    def test_unknown_theme_falls_back_to_dark(self):
        self.assertIs(th.tokens("nope"), th.THEMES["dark"])

    def test_qss_names_the_widgets_and_uses_the_tokens(self):
        css = th.qss("light")
        for obj in ("ChatWindow", "Header", "Composer", "Send", "Interrupt", "Suggestion", "PermissionSheet", "Deny", "Allow"):
            self.assertIn(obj, css)
        self.assertIn(th.THEMES["light"]["bg"], css)
        self.assertNotIn("border-radius: 4", css)
        self.assertNotIn("{t[", css)          # no unformatted placeholder left behind


class HtmlTest(unittest.TestCase):
    def test_user_text_is_escaped(self):
        out = th.user_html("<script>alert(1)</script>\nline 2")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<br>", out)
        self.assertIn(th.THEMES["dark"]["user_bg"], out)

    def test_assistant_bold_inline_code_and_fenced_block(self):
        out = th.assistant_html("Use **this** and `x < y`.\n```python\nif a < b:\n    pass\n```\nDone.")
        self.assertIn("<b>this</b>", out)
        self.assertIn("&nbsp;x &lt; y&nbsp;", out)
        self.assertIn("<pre", out)
        self.assertIn("if a &lt; b:", out)
        self.assertIn("Done.", out)

    def test_assistant_markup_cannot_inject_html(self):
        self.assertNotIn("<img", th.assistant_html("<img src=x onerror=1>"))

    def test_tool_line_splits_name_and_argument(self):
        out = th.tool_html("Read: a <b>.txt")
        self.assertIn("<b>Read</b>", out)
        self.assertIn("a &lt;b&gt;.txt", out)

    def test_notice_label_and_retry_link(self):
        out = th.notice_html("error", "Error: it broke", link=("Try again", "nina:retry"))
        self.assertIn("<b>Error:</b> it broke", out)
        self.assertIn('href="nina:retry"', out)
        self.assertIn(th.THEMES["dark"]["err_bg"], out)
        self.assertIn(th.THEMES["dark"]["warn_bg"], th.notice_html("blocked", "Blocked (Bash): no"))

    def test_light_theme_uses_light_tokens(self):
        self.assertIn(th.THEMES["light"]["user_bg"], th.user_html("x", "light"))


if __name__ == "__main__":
    unittest.main()
