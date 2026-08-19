# -*- coding: utf-8 -*-
"""
parts_vision.py — 配件商品图像智能校验（CLIP 零样本图文匹配）
====================================================================
用途：在 fetch_parts 抓取配件报价时，对候选商品的展示图（picUrl）做
      机器学习判断——确认图里拍的是不是关键词商品本身（如"摩托车链条"），
      而不是齿轮、锁、机器、图纸、Logo 等串扰内容，确认后才展示。

原理：CLIP（Contrastive Language-Image Pre-training）视觉-语言模型，
      将商品图与"正例/负例"文本描述分别编码到同一向量空间，
      计算余弦相似度。若目标商品文本与图的相似度高于所有负例，则判定通过。
      属于零样本（zero-shot）分类：不需要训练数据，换关键词只需换文本标签。

模型：Xenova/clip-vit-base-patch32 量化 ONNX 版（纯 CPU 推理，单图约 100ms）
      存放于 ./models/clip/：
        - vision_model_quantized.onnx   图像编码器（约 90MB）
        - text_model_quantized.onnx     文本编码器（约 63MB）
        - tokenizer.json                CLIP BPE 分词器

依赖：onnxruntime / numpy / pillow / tokenizers（装在隔离 venv 中）。
      依赖缺失或模型未下载时，模块会优雅降级，trade_monitor 回退到纯规则过滤。
"""

import math
import os
import urllib.request

# 模型目录（与 trade_monitor.py 同级的 models/clip/）
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "clip")

# 判定通过需要同时满足：
#   1) 目标商品的 cos_sim 必须严格高于所有负例（argmax 为正）
#   2) 在全部标签的 softmax 中，正例总概率不低于 MIN_CONF
# 第二个条件防止边缘 case（例如 0.250 vs 0.249 的微弱优势）。
MIN_CONF = 0.30

# 关键词核心词 → 英文商品标签（CLIP ViT-B/32 是英文预训练，中文标签效果差，故先映射）
PART_LABEL_EN = {
    "刹车片": "brake pads for a motorcycle",
    "链条":   "a motorcycle roller chain",
    "轮胎":   "a motorcycle tire",
    "火花塞": "spark plugs",
    "机油滤芯": "an oil filter for motorcycle",
    "空气滤芯": "an air filter",
    "电瓶":   "a motorcycle battery",
    "后视镜": "a motorcycle rearview mirror",
    "大灯":   "a motorcycle headlight",
    "离合器片": "a clutch friction plate",
    "整流器": "a motorcycle voltage regulator",
    "高压包": "a motorcycle ignition coil",
}

# 通用负例：无论什么关键词，这些都不是"目标商品本身的实拍图"
GENERIC_NEGATIVES = [
    "a company logo",
    "text on a white background",
    "a technical drawing or diagram",
    "a factory workshop",
    "a person",
    "a package box",
]

# 关键词专属负例：历史上实际串扰进结果、需要靠图像区分的品类
KEYWORD_NEGATIVES = {
    "链条": [
        "a metal gear", "a sprocket", "a padlock", "a chain lock",
        "a bicycle cable lock", "a snow chain", "a chainsaw", "machine oil",
        "an industrial machine", "a motorcycle chain tensioner",
    ],
    "刹车片": [
        "a brake disc", "a metal spring", "railway brake shoes",
        "a brake caliper", "friction material", "a car brake pad",
    ],
    "轮胎": [
        "an inner tube", "a car tire", "a tractor tire",
        "a motorcycle rim", "a wheel hub",
    ],
    "火花塞": [
        "an ignition coil", "spark plug wires", "engine pistons",
    ],
    "机油滤芯": [
        "an air filter", "a cabin filter", "motor oil bottles", "an oil can",
    ],
}

# 进程内单例：模型会话只初始化一次
_CLIP = None


class _ClipRuntime:
    """CLIP 推理运行时：图像/文本编码器 + 分词器，懒加载。"""

    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        from PIL import Image
        from tokenizers import Tokenizer

        self.np = np
        self.Image = Image
        vpath = os.path.join(MODEL_DIR, "vision_model_quantized.onnx")
        tpath = os.path.join(MODEL_DIR, "text_model_quantized.onnx")
        kpath = os.path.join(MODEL_DIR, "tokenizer.json")
        for p in (vpath, tpath, kpath):
            if not os.path.exists(p):
                raise FileNotFoundError(f"模型文件缺失: {p}")
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        self.vision = ort.InferenceSession(vpath, so, providers=["CPUExecutionProvider"])
        self.text = ort.InferenceSession(tpath, so, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(kpath)
        self._text_cache = {}       # 文本标签 → 归一化嵌入（标签固定，避免重复编码）
        self._img_cache = {}        # URL → (PIL Image, embedding)（同一轮抓取中复用）

    def encode_image(self, img_or_url):
        """
        支持传入 PIL.Image 或图片 URL（自动下载并缓存）。
        返回归一化 512 维图像嵌入。
        """
        np = self.np
        if isinstance(img_or_url, str):
            url = img_or_url
            if url in self._img_cache:
                img = self._img_cache[url][0]
            else:
                img = self._download_image(url)
                self._img_cache[url] = (img, None)
        else:
            img = img_or_url
            url = None

        # 标准 CLIP 预处理：224×224，ImageNet-1k 风格的 mean/std
        img = img.convert("RGB").resize((224, 224))
        x = np.asarray(img, dtype=np.float32) / 255.0
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        x = (x - mean) / std
        x = x.transpose(2, 0, 1)[None]   # HWC → NCHW
        out = self._run(self.vision, {"pixel_values": x})
        emb = self._pick_embed(out)
        vec = self._norm(emb[0])
        if url:
            self._img_cache[url] = (img, vec)
        return vec

    def encode_text(self, prompt):
        """文本 → 归一化 512 维文本嵌入（带缓存）。"""
        if prompt in self._text_cache:
            return self._text_cache[prompt]
        np = self.np
        enc = self.tok.encode(prompt)
        ids = list(enc.ids)
        # 兼容不同 tokenizer 后处理器：确保 BOS(49406)/EOS(49407) 存在且截断到 77
        if not ids or ids[0] != 49406:
            ids = [49406] + ids
        if ids[-1] != 49407:
            ids = ids + [49407]
        ids = ids[:77]
        arr = np.array([ids], dtype=np.int64)
        feed = {"input_ids": arr}
        out = self._run(self.text, feed)
        emb = self._pick_embed(out)
        vec = self._norm(emb[0])
        self._text_cache[prompt] = vec
        return vec

    def _download_image(self, url, timeout=15):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://b2b.baidu.com/",
        })
        import io
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        return self.Image.open(io.BytesIO(data))

    @staticmethod
    def _run(session, feed):
        """只喂会话声明的输入（避免文本模型收到未声明的 attention_mask）。"""
        names = {i.name for i in session.get_inputs()}
        full = {k: v for k, v in feed.items() if k in names}
        if "position_ids" in names and "position_ids" not in full:
            np = __import__("numpy")
            full["position_ids"] = np.arange(feed["input_ids"].shape[1],
                                             dtype=np.int64)[None]
        return session.run(None, full)

    @staticmethod
    def _pick_embed(outputs):
        """ONNX 输出优先取 *_embeds，否则取最后一个。"""
        return outputs[-1]

    def _norm(self, vec):
        n = math.sqrt(float((vec * vec).sum())) or 1.0
        return vec / n


def _get_clip():
    """获取 CLIP 运行时单例；依赖或模型缺失时返回 None（调用方降级）。"""
    global _CLIP
    if _CLIP is None:
        try:
            _CLIP = _ClipRuntime()
            print(f"[vision] CLIP 加载成功（{len(_CLIP._text_cache)} 个缓存标签）")
        except Exception as e:
            print(f"[vision] CLIP 初始化失败（将降级为纯规则过滤）: {e}")
            _CLIP = False
    return _CLIP or None


def label_for_keyword(kw):
    """关键词 → 英文商品标签；未映射的关键词回退为通用标签。"""
    core = kw.replace("重庆", "").replace(" ", "")
    for v in ("摩托车", "汽车", "电动车", "三轮车", "踏板车", "卡车", "重卡", "通机"):
        if core.startswith(v) and len(core) > len(v):
            core = core[len(v):]
            break
    if core in PART_LABEL_EN:
        return PART_LABEL_EN[core], core
    return "vehicle spare parts", core


def check_image(img_url, keyword, min_conf=None):
    """
    用 CLIP 对商品图做零样本分类，判断是否为 keyword 商品本身。

    返回:
        (True,  confidence)   模型确认是目标商品
        (False, confidence)   模型判定不是目标商品
        (None,  None)         模型不可用 / 图片下载失败（调用方应降级到规则过滤）

    confidence 为「目标商品」在全部候选标签 softmax 中的概率。
    """
    clip = _get_clip()
    if clip is None or not img_url:
        return None, None
    min_conf = MIN_CONF if min_conf is None else min_conf

    try:
        img_vec = clip.encode_image(img_url)
    except Exception:
        # 图片 404 / 防盗链 / 非图格式 → 不因此误杀
        return None, None

    label, core = label_for_keyword(keyword)
    positives = [
        f"a product photo of {label}",
        f"a close-up photo of {label}",
    ]
    negatives = [f"a product photo of {n}" for n in GENERIC_NEGATIVES]
    negatives += [f"a product photo of {n}" for n in KEYWORD_NEGATIVES.get(core, [])]

    pos_vecs = [clip.encode_text(t) for t in positives]
    neg_vecs = [clip.encode_text(t) for t in negatives]

    np = clip.np
    pos_sims = np.array([float((img_vec * v).sum()) for v in pos_vecs])
    neg_sims = np.array([float((img_vec * v).sum()) for v in neg_vecs])

    # softmax 计算置信度（CLIP 默认温度 100）
    all_sims = np.concatenate([pos_sims, neg_sims]) * 100.0
    e = np.exp(all_sims - all_sims.max())
    probs = e / e.sum()
    confidence = float(probs[:len(positives)].sum())

    # 判定：最相似的标签必须是正例，且正例总概率不低于阈值
    ok = bool(pos_sims.max() > neg_sims.max()) and confidence >= min_conf
    return ok, round(confidence, 4)


if __name__ == "__main__":
    print("初始化 CLIP ...")
    c = _get_clip()
    if not c:
        print("CLIP 不可用，退出")
        raise SystemExit(1)
    print("模型就绪。示例：")
    import sys
    tests = [
        ("摩托车链条", "https://img0.baidu.com/5a1168f0.jpg"),   # 占位
    ]
    if len(sys.argv) > 2:
        tests = [(sys.argv[1], u) for u in sys.argv[2:]]
    for kw, url in tests:
        ok, p = check_image(url, kw)
        print(f"  {kw}  {'✓通过' if ok else '✗否决' if ok is not None else '?跳过'}  p={p}")
