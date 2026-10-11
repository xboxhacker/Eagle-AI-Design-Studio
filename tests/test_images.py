"""Pictures in the chat: pasted / dropped into the message box, scaled, sent to Claude and ChatGPT/Grok in their
formats, saved with the chat, shown again when the chat is reopened, mentioned (not resent) in later history."""
import base64, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtCore import QMimeData, QSettings, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

tmp = Path(tempfile.mkdtemp())
QSettings.setDefaultFormat(QSettings.IniFormat)                       # never touch the user's real settings
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "qs"))
for n in ("question", "information", "warning"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
app = QApplication.instance() or QApplication([])

from eagleai import images as im
from fakes import FakeProvider


def picture(w, h, color="#2f6fdb"):
    img = QImage(w, h, QImage.Format_RGB32); img.fill(QColor(color))
    return img


def test_scaled_and_encoded():
    a = im.from_qimage(picture(4000, 2000), "big")
    assert (a["w"], a["h"]) == (1568, 784) and a["media_type"] == "image/png"
    assert QImage.fromData(a["data"]).width() == 1568
    assert im.tokens(a) == int(1568 * 784 / 750)
    assert im.data_url(a).startswith("data:image/png;base64,")
    assert im.from_qimage(QImage()) is None


def test_provider_wire_formats():
    from eagleai.providers import ClaudeProvider, ResponsesProvider
    a = im.from_qimage(picture(40, 30))
    msgs = ClaudeProvider("k", "claude-x").start([], "what is this?", [a])
    content = msgs[-1]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[0]["source"]["data"]) == a["data"]
    assert content[-1] == {"type": "text", "text": "what is this?"}
    items = ResponsesProvider("k", "gpt-x", "openai").start([], "what is this?", [a])
    c = items[-1]["content"]
    assert c[0] == {"type": "input_text", "text": "what is this?"}
    assert c[1]["type"] == "input_image" and c[1]["image_url"] == im.data_url(a)
    assert ResponsesProvider("k", "gpt-x", "openai").start([], "plain")[-1]["content"] == "plain"   # unchanged


from eagleai.app import MainWindow
from eagleai.tools import Project, Settings
w = MainWindow()


def test_agent_saves_and_history_mentions():
    from eagleai.agent import Agent
    p = Project(tmp / "proj", Settings(lib_dirs=[]))
    ag = Agent(p)
    fp = FakeProvider([("I see a blue square", [])])
    a = im.from_qimage(picture(64, 48), "shot")
    ag.run("look", provider=fp, images=[a])
    assert fp.seen_images == [a]
    entry = next(e for e in ag.log if e.get("t") == "user")
    saved = p.folder / entry["images"][0]
    assert saved.exists() and saved.parent.name == "chat_images" and QImage(str(saved)).width() == 64
    fp2 = FakeProvider([("ok", [])])
    ag.run("and now?", provider=fp2)
    assert fp2.seen_images is None and "1 image(s) were attached" in fp2.seen_history[0]["text"]
    w.open_project(p.folder)                                # reopened chat shows the picture again
    for _ in range(50):
        app.processEvents(); time.sleep(0.01)
    assert "<img" in w.log.toHtml() and "not found" not in w.log.toHtml()


def test_paste_and_drop_into_message_box():
    w.attachments = []; w._show_attachments()
    md = QMimeData(); md.setImageData(picture(300, 200))            # a screenshot (Win+Shift+S)
    w.input.insertFromMimeData(md)
    assert len(w.attachments) == 1 and w.attach_bar.isVisibleTo(w) and w.input.toPlainText() == ""
    f = tmp / "photo.png"; picture(50, 50, "#ff0000").save(str(f))
    md = QMimeData(); md.setUrls([QUrl.fromLocalFile(str(f))])     # a file copied in Explorer / dropped
    w.input.insertFromMimeData(md)
    assert len(w.attachments) == 2 and w.attachments[1]["name"] == "photo.png"
    md = QMimeData(); md.setText("R1 10k"); md.setImageData(picture(10, 10))   # Word/Excel: text + picture
    w.input.insertFromMimeData(md)
    assert len(w.attachments) == 2 and w.input.toPlainText() == "R1 10k"
    assert "image" in w.est_lbl.text()
    w._remove_attachment(0)
    assert len(w.attachments) == 1 and w.attachments[0]["name"] == "photo.png"
    w.attachments = []; w._show_attachments()
    assert not w.attach_bar.isVisibleTo(w)


if __name__ == "__main__":
    import traceback
    rc = 0
    try:
        for n, fn in list(globals().items()):
            if n.startswith("test_"):
                fn(); print("ok", n)
    except Exception:
        traceback.print_exc(); rc = 1
    sys.stdout.flush(); sys.stderr.flush()
    if os.name == "nt":                     # end like the app does: Qt teardown with live workers can crash
        from eagleai.app import terminate_self
        terminate_self(rc)
    os._exit(rc)
