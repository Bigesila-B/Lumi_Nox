"""部署层冒烟测试：设置持久化 + 发声器生命周期 + 皮套 zip 导入。

仅依赖标准库（show_settings / tts_emitter 均零第三方依赖）；
上游 CI 环境若无部署层文件则整组跳过。
"""
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import show_settings
    from tts_emitter import IndependentTTSEmitter
except ImportError:  # 上游 CI 没有部署层文件
    show_settings = None
    IndependentTTSEmitter = None


class FakeSynth:
    """模拟 MimoSynth：一次发声一条流，finish() 后自行关闭。"""

    def __init__(self):
        self.opened = False
        self.opens = 0
        self.fed = []
        self.finished = 0
        self.aborted = 0

    def open(self, voice_id, model, cable_index):
        self.opened = True
        self.opens += 1

    def feed(self, sentence):
        if not self.opened:
            raise RuntimeError("流未打开")
        self.fed.append(sentence)

    def finish(self):
        self.finished += 1
        self.opened = False  # 与 MimoSynth 行为一致：finish 后关闭

    def abort(self):
        self.aborted += 1
        self.opened = False


@unittest.skipUnless(IndependentTTSEmitter, "部署层文件不存在")
class IndependentTTSEmitterLifecycle(unittest.TestCase):
    """回归：finish() 后包装层必须复位 _opened，否则同一角色第二次发声时
    feed() 误以为链路仍开，所有句子被静默丢弃（"聊几句就没声音"的根因）。"""

    def test_second_speak_reopens_stream(self):
        synth = FakeSynth()
        em = IndependentTTSEmitter(synth, voice_id="茉莉",
                                   model="mimo-v2.5-tts", cable_index=None)
        em.feed("第一句")
        em.finish()
        self.assertEqual(synth.opens, 1)
        em.feed("第二句")  # 修复前：这里不会重新 open，句子被静默丢弃
        self.assertEqual(synth.fed, ["第一句", "第二句"])
        self.assertEqual(synth.opens, 2)
        em.finish()
        self.assertEqual(synth.finished, 2)
        self.assertFalse(synth.opened)

    def test_abort_resets_open_flag(self):
        synth = FakeSynth()
        em = IndependentTTSEmitter(synth, voice_id="茉莉",
                                   model="mimo-v2.5-tts", cable_index=None)
        em.feed("x")
        em.abort()
        self.assertFalse(synth.opened)
        em.feed("y")  # abort 后仍可重新打开
        self.assertEqual(synth.fed[-1], "y")


@unittest.skipUnless(show_settings, "部署层文件不存在")
class ShowSettingsPersistence(unittest.TestCase):
    def setUp(self):
        self._old_file = show_settings.SETTINGS_FILE
        self._old_clones_dir = show_settings.CLONES_DIR
        self._tmp = tempfile.TemporaryDirectory()
        show_settings.SETTINGS_FILE = Path(self._tmp.name) / "show_settings.json"
        show_settings.CLONES_DIR = Path(self._tmp.name) / "cloned_voices"
        show_settings.load()

    def tearDown(self):
        show_settings.SETTINGS_FILE = self._old_file
        show_settings.CLONES_DIR = self._old_clones_dir
        show_settings.load()
        self._tmp.cleanup()

    def test_defaults_without_file(self):
        s = show_settings.get()
        self.assertEqual(s["stage"]["amplitude"], 1.5)
        self.assertEqual(s["tts"]["volume"], 1.0)
        self.assertEqual(s["clones"], {})

    def test_save_and_reload_roundtrip(self):
        show_settings.save({"llm": {"model": "test-model", "temperature": 1.7},
                            "stage": {"subtitle": False, "amplitude": 2.0}})
        s = show_settings.load()
        self.assertEqual(s["llm"]["model"], "test-model")
        self.assertEqual(s["llm"]["temperature"], 1.7)  # 通用钳制 [0, 2]
        self.assertFalse(s["stage"]["subtitle"])
        self.assertEqual(s["stage"]["amplitude"], 2.0)
        self.assertEqual(s["clones"], {})

    def test_clone_lifecycle_and_data_uri(self):
        cdir = Path(self._tmp.name) / "cloned_voices"
        cdir.mkdir()
        (cdir / "ref.wav").write_bytes(b"RIFFfake-audio")
        show_settings.add_clone("我的音色", "cloned_voices/ref.wav", "audio/wav")
        clone = show_settings.get_clone("我的音色")
        self.assertIsNotNone(clone)
        uri = show_settings.clone_data_uri(clone)
        self.assertTrue(uri.startswith("data:audio/wav;base64,"))
        self.assertIn(base64.b64encode(b"RIFFfake-audio").decode(), uri)
        self.assertTrue(show_settings.is_clone_voice("clone:我的音色"))
        self.assertEqual(show_settings.clone_name("clone:我的音色"), "我的音色")
        self.assertTrue(show_settings.delete_clone("我的音色"))
        self.assertIsNone(show_settings.get_clone("我的音色"))
        self.assertFalse(show_settings.delete_clone("我的音色"))  # 重复删除返回 False


@unittest.skipUnless(show_settings, "部署层文件不存在")
class ModelZipImport(unittest.TestCase):
    """皮套导入：解压进 live2d/、包装目录剥离、完整性校验、穿越拒绝与回滚。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.web = Path(self._tmp.name) / "live2d"
        self.web.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _zip(**members) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()

    def _model3(self, moc="m.moc3", tex="t.png"):
        return json.dumps({"FileReferences": {"Moc": moc, "Textures": [tex]}})

    def test_import_strips_wrapper_and_scan_finds_it(self):
        data = self._zip(**{
            "Hiyori/hiyori.model3.json": self._model3(),
            "Hiyori/m.moc3": b"moc",
            "Hiyori/t.png": b"png",
        })
        r = show_settings.import_model_zip(data, "hiyori", self.web)
        # 整包只有一个顶层目录 → 剥掉这层冗余包装，直接落到 live2d/hiyori/
        self.assertEqual(r["dir"], "hiyori")
        self.assertEqual(r["models"],
                         [{"path": "/hiyori/hiyori.model3.json",
                           "label": "hiyori/hiyori.model3.json"}])
        paths = [m["path"] for m in show_settings.scan_models(self.web)]
        self.assertIn("/hiyori/hiyori.model3.json", paths)

    def test_import_rejects_missing_texture_and_rolls_back(self):
        data = self._zip(**{
            "m.model3.json": self._model3(tex="missing.png"),
            "m.moc3": b"moc",
        })
        with self.assertRaises(ValueError):
            show_settings.import_model_zip(data, "bad", self.web)
        self.assertFalse((self.web / "bad").exists())  # 不留半成品

    def test_import_rejects_zip_slip(self):
        data = self._zip(**{"../evil.txt": b"x",
                            "m.model3.json": self._model3(),
                            "m.moc3": b"moc", "t.png": b"png"})
        with self.assertRaises(ValueError):
            show_settings.import_model_zip(data, "evil", self.web)
        # 压缩包外的文件必须原封不动
        self.assertFalse((self.web.parent / "evil.txt").exists())

    def test_import_rejects_non_zip_and_model_less_archive(self):
        with self.assertRaises(ValueError):
            show_settings.import_model_zip(b"not a zip", "x", self.web)
        with self.assertRaises(ValueError):
            show_settings.import_model_zip(self._zip(readme=b"hi"),
                                           "nomodel", self.web)

    def test_import_duplicate_name_gets_timestamp_suffix(self):
        data = self._zip(**{
            "hiyori.model3.json": self._model3(),
            "m.moc3": b"moc", "t.png": b"png"})
        first = show_settings.import_model_zip(data, "hiyori", self.web)
        second = show_settings.import_model_zip(data, "hiyori", self.web)
        self.assertEqual(first["dir"], "hiyori")
        self.assertTrue(second["dir"].startswith("hiyori_"))
        self.assertNotEqual(first["dir"], second["dir"])
        # 两次导入互不影响，scan 全部可见
        self.assertEqual(len(show_settings.scan_models(self.web)), 2)


if __name__ == "__main__":
    unittest.main()
