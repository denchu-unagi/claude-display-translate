from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\((?!https?:)([^)#\s]*)(?:#([^)\s]*))?\)")


def anchors(path):
    # GitHub が見出しから作るアンカーの近似。
    headings = re.findall(r"^#+ (.+)$", path.read_text(encoding="utf-8"), re.M)
    return {re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-") for heading in headings}


class DocumentLinkTest(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        # README と docs/ の間のリンクは、見出しの書き換えで黙って切れるため確かめる。
        documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        for document in documents:
            for target, anchor in LINK.findall(document.read_text(encoding="utf-8")):
                destination = (document.parent / target).resolve() if target else document
                with self.subTest(document=document.name, link=f"{target}#{anchor}"):
                    self.assertTrue(destination.exists())
                    if anchor:
                        self.assertIn(anchor, anchors(destination))


if __name__ == "__main__":
    unittest.main()
