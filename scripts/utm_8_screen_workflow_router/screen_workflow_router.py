#!/usr/bin/env python3
"""在当前屏幕（或一张指定图片）中查找黑白 OpenCV 模板。

示例：
    python3 -m pip install opencv-python pillow
    python3 screen_workflow_router.py --template ./templates/apple-account.png
    python3 screen_workflow_router.py --template ./templates/apple-account.png --click
    python3 screen_workflow_router.py --workflow ./workflows/apple-account-navigation.json

默认匹配模板和屏幕截图的黑白边缘轮廓，因此可直接使用黑白模板，并能
抵抗悬停时的背景变蓝、文字变白等前景/背景反转。模板与屏幕截图像素
比例不一致（例如 Retina 截图）时，可通过 ``--scales 1,2`` 同时尝试
多个缩放比例。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence


class TemplateMatchError(RuntimeError):
    """模板匹配的可读错误。"""


CLICK_REVERIFY_DELAY_SECONDS = 3.0
DEFAULT_WAIT_AFTER_CLICK_SECONDS = 3.0
MATCH_MODES = ("edges", "binary")


def parse_scales(value: str) -> list[float]:
    """解析逗号分隔的模板缩放比例。"""
    try:
        scales = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--scales 必须是逗号分隔的正数") from exc

    if not scales or any(scale <= 0 for scale in scales):
        raise argparse.ArgumentTypeError("--scales 至少包含一个正数")
    return scales


def parse_region(value: str) -> tuple[int, int, int, int]:
    """解析 x,y,width,height 格式的屏幕区域。"""
    try:
        values = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--region 格式应为 x,y,width,height") from exc

    if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
        raise argparse.ArgumentTypeError("--region 格式应为 x,y,width,height，宽高必须大于 0")
    return values


def require_cv2():
    """延迟导入 OpenCV，使 --help 在未安装依赖时仍然可用。"""
    try:
        import cv2
    except ModuleNotFoundError as exc:
        raise TemplateMatchError(
            "缺少 OpenCV。请先运行：python3 -m pip install opencv-python pillow"
        ) from exc
    return cv2


def require_image_grab():
    """延迟导入 Pillow 的屏幕截图模块。"""
    try:
        from PIL import ImageGrab
    except ModuleNotFoundError as exc:
        raise TemplateMatchError(
            "缺少 Pillow。请先运行：python3 -m pip install pillow"
        ) from exc
    return ImageGrab


def require_pyautogui():
    """延迟导入鼠标控制库，避免普通匹配依赖辅助功能权限。"""
    try:
        import pyautogui
    except ModuleNotFoundError as exc:
        raise TemplateMatchError(
            "缺少 PyAutoGUI。请先运行：.venv/bin/python -m pip install pyautogui"
        ) from exc
    return pyautogui


def read_image(cv2, image_path: Path):
    """读取图像，保留对透明 PNG 的兼容性。"""
    if not image_path.is_file():
        raise TemplateMatchError(f"找不到图片：{image_path}")
    image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise TemplateMatchError(f"无法读取图片：{image_path}")
    return image


def capture_screen(region: tuple[int, int, int, int] | None):
    """截取当前所有显示器合成的屏幕，并返回 BGR 图像和区域原点。"""
    ImageGrab = require_image_grab()
    try:
        if region is None:
            screenshot = ImageGrab.grab(all_screens=True)
            origin = (0, 0)
        else:
            x, y, width, height = region
            screenshot = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
            origin = (x, y)
    except OSError as exc:
        raise TemplateMatchError(
            "截屏失败。请在 macOS“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”中"
            "允许当前 Terminal 或 Python。"
        ) from exc

    import numpy as np

    # Pillow 是 RGB/RGBA，OpenCV 使用 BGR/BGRA。
    pixels = np.asarray(screenshot)
    if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
        raise TemplateMatchError("截屏返回的颜色格式不受支持")
    return pixels[:, :, :3][:, :, ::-1].copy(), origin


def as_gray(cv2, image):
    """将 BGR/BGRA 图像转为灰度图。"""
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def as_black_and_white(cv2, image):
    """用 Otsu 自动阈值将输入统一为纯黑白二值图。"""
    gray = as_gray(cv2, image)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return binary


def as_edges(cv2, image):
    """将输入转换为黑白轮廓图，保留形状并忽略颜色和明暗极性。"""
    gray = as_gray(cv2, image)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.Canny(blurred, 50, 150)


def preprocess_for_match(cv2, image, match_mode: str):
    """根据匹配策略生成黑白特征图。"""
    if match_mode == "edges":
        return as_edges(cv2, image)
    if match_mode == "binary":
        return as_black_and_white(cv2, image)
    raise TemplateMatchError(f"不支持的匹配模式：{match_mode}")


def iter_scaled_templates(cv2, template, scales: Iterable[float]):
    """产生每个有效缩放比例下的模板。"""
    original_height, original_width = template.shape[:2]
    for scale in scales:
        width = max(1, round(original_width * scale))
        height = max(1, round(original_height * scale))
        if width == original_width and height == original_height:
            yield scale, template
            continue
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        yield scale, cv2.resize(template, (width, height), interpolation=interpolation)


def find_best_match(cv2, screen, template, scales: Sequence[float], match_mode: str):
    """在所有缩放候选中返回分数最高的一项。"""
    screen_features = preprocess_for_match(cv2, screen, match_mode)
    best_match = None

    for scale, scaled_template in iter_scaled_templates(cv2, template, scales):
        template_features = preprocess_for_match(cv2, scaled_template, match_mode)
        height, width = template_features.shape[:2]
        if (
            height > screen_features.shape[0]
            or width > screen_features.shape[1]
        ):
            continue

        result = cv2.matchTemplate(
            screen_features,
            template_features,
            cv2.TM_CCOEFF_NORMED,
        )
        _, score, _, location = cv2.minMaxLoc(result)
        candidate = {
            "score": float(score),
            "x": int(location[0]),
            "y": int(location[1]),
            "width": int(width),
            "height": int(height),
            "scale": float(scale),
            "match_mode": match_mode,
        }
        if best_match is None or candidate["score"] > best_match["score"]:
            best_match = candidate

    if best_match is None:
        raise TemplateMatchError("模板的每个缩放尺寸都大于待匹配图片")
    return best_match


def select_unique_match(
    candidates: Sequence[dict[str, float | int]],
    threshold: float,
) -> dict[str, float | int]:
    """只接受唯一达到阈值的候选。

    这是原路由器“只取 best match”的安全加固：多处相同文本或模板
    同时命中时不猜测点击位置。
    """
    eligible = [candidate for candidate in candidates if float(candidate["score"]) >= threshold]
    if not eligible:
        raise TemplateMatchError("模板未达到匹配阈值")
    if len(eligible) != 1:
        raise TemplateMatchError(
            f"模板匹配不唯一：共 {len(eligible)} 个候选达到阈值"
        )
    return dict(eligible[0])


def _same_visual_target(left: dict[str, float | int], right: dict[str, float | int]) -> bool:
    """判断不同缩放比例是否命中同一屏幕目标。"""
    left_center = (
        float(left["x"]) + float(left["width"]) / 2,
        float(left["y"]) + float(left["height"]) / 2,
    )
    right_center = (
        float(right["x"]) + float(right["width"]) / 2,
        float(right["y"]) + float(right["height"]) / 2,
    )
    tolerance_x = max(float(left["width"]), float(right["width"])) / 3
    tolerance_y = max(float(left["height"]), float(right["height"])) / 3
    return (
        abs(left_center[0] - right_center[0]) <= tolerance_x
        and abs(left_center[1] - right_center[1]) <= tolerance_y
    )


def find_threshold_matches(
    cv2,
    screen,
    template,
    scales: Sequence[float],
    match_mode: str,
    threshold: float,
) -> list[dict[str, float | int]]:
    """收集各缩放比例的局部极大值，并合并同一视觉目标。"""
    import numpy as np

    screen_features = preprocess_for_match(cv2, screen, match_mode)
    raw_candidates: list[dict[str, float | int]] = []
    for scale, scaled_template in iter_scaled_templates(cv2, template, scales):
        template_features = preprocess_for_match(cv2, scaled_template, match_mode)
        height, width = template_features.shape[:2]
        if height > screen_features.shape[0] or width > screen_features.shape[1]:
            continue
        scores = cv2.matchTemplate(
            screen_features,
            template_features,
            cv2.TM_CCOEFF_NORMED,
        )
        local_maxima = cv2.dilate(scores, np.ones((3, 3), dtype=np.uint8))
        rows, columns = np.where((scores >= threshold) & (scores >= local_maxima - 1e-12))
        for y, x in zip(rows.tolist(), columns.tolist()):
            raw_candidates.append(
                {
                    "score": float(scores[y, x]),
                    "x": int(x),
                    "y": int(y),
                    "width": int(width),
                    "height": int(height),
                    "scale": float(scale),
                    "match_mode": match_mode,
                }
            )

    unique_candidates: list[dict[str, float | int]] = []
    for candidate in sorted(raw_candidates, key=lambda item: float(item["score"]), reverse=True):
        if any(_same_visual_target(candidate, existing) for existing in unique_candidates):
            continue
        unique_candidates.append(candidate)
    return unique_candidates


def locate_template(
    cv2,
    template,
    image_path,
    region,
    scales,
    match_mode: str,
    threshold: float,
):
    """在一张图片或刚截取的屏幕中定位模板，并换算为全局坐标。"""
    if image_path is not None:
        screen = read_image(cv2, image_path)
        origin = (0, 0)
    else:
        screen, origin = capture_screen(region)

    candidates = find_threshold_matches(
        cv2, screen, template, scales, match_mode, threshold
    )
    match = select_unique_match(candidates, threshold)
    match["candidate_count"] = len(candidates)
    match["x"] += origin[0]
    match["y"] += origin[1]
    # 模板左上角不一定是可点击区域；默认点击中心点。
    match["click_x"] = match["x"] + match["width"] // 2
    match["click_y"] = match["y"] + match["height"] // 2
    return screen, origin, match


def save_annotated_image(cv2, image, match: dict[str, float | int], output_path: Path) -> None:
    """在副本上框出命中位置，避免修改原图。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    annotated = image.copy()
    x, y = int(match["x"]), int(match["y"])
    width, height = int(match["width"]), int(match["height"])
    cv2.rectangle(annotated, (x, y), (x + width, y + height), (0, 255, 0), 2)
    cv2.putText(
        annotated,
        f"{match['score']:.3f}",
        (x, max(22, y - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    if not cv2.imwrite(str(output_path), annotated):
        raise TemplateMatchError(f"无法保存标注图：{output_path}")


def validate_threshold(value, field_name: str) -> float:
    """校验并统一阈值类型。"""
    if isinstance(value, bool):
        raise TemplateMatchError(f"{field_name} 必须是 0 到 1 之间的数字")
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise TemplateMatchError(f"{field_name} 必须是 0 到 1 之间的数字") from exc
    if not 0 <= threshold <= 1:
        raise TemplateMatchError(f"{field_name} 必须在 0 到 1 之间")
    return threshold


def validate_scale_list(value, field_name: str) -> list[float]:
    """校验工作流中的缩放比例数组。"""
    if not isinstance(value, list) or not value:
        raise TemplateMatchError(f"{field_name} 必须是至少含一个正数的数组")
    try:
        scales = [float(scale) for scale in value]
    except (TypeError, ValueError) as exc:
        raise TemplateMatchError(f"{field_name} 必须是至少含一个正数的数组") from exc
    if any(scale <= 0 for scale in scales):
        raise TemplateMatchError(f"{field_name} 只能包含正数")
    return scales


def validate_match_mode(value, field_name: str) -> str:
    """校验模板特征提取方式。"""
    if not isinstance(value, str) or value not in MATCH_MODES:
        supported = ", ".join(MATCH_MODES)
        raise TemplateMatchError(f"{field_name} 只能是：{supported}")
    return value


def validate_wait_after_click_seconds(value, field_name: str) -> float:
    """校验节点点击后、执行下一节点前的等待时长。"""
    if isinstance(value, bool):
        raise TemplateMatchError(f"{field_name} 必须是大于等于 0 的秒数")
    try:
        wait_seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise TemplateMatchError(f"{field_name} 必须是大于等于 0 的秒数") from exc
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise TemplateMatchError(f"{field_name} 必须是大于等于 0 的秒数")
    return wait_seconds


def load_workflow(workflow_path: Path) -> tuple[str, list[dict[str, object]]]:
    """读取并严格校验按顺序执行的模板点击工作流。"""
    if not workflow_path.is_file():
        raise TemplateMatchError(f"找不到工作流文件：{workflow_path}")
    try:
        payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateMatchError(f"无法读取工作流 JSON：{workflow_path}") from exc

    if not isinstance(payload, dict):
        raise TemplateMatchError("工作流根节点必须是 JSON 对象")
    workflow_name = payload.get("name", workflow_path.stem)
    if not isinstance(workflow_name, str) or not workflow_name.strip():
        raise TemplateMatchError("工作流 name 必须是非空字符串")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TemplateMatchError("工作流必须包含非空 steps 数组")

    steps: list[dict[str, object]] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        prefix = f"steps[{index}]"
        if not isinstance(raw_step, dict):
            raise TemplateMatchError(f"{prefix} 必须是对象")
        name = raw_step.get("name", f"step-{index}")
        template = raw_step.get("template")
        verify_template = raw_step.get("verify_template")
        click = raw_step.get("click", True)
        if not isinstance(name, str) or not name.strip():
            raise TemplateMatchError(f"{prefix}.name 必须是非空字符串")
        if not isinstance(template, str) or not template.strip():
            raise TemplateMatchError(f"{prefix}.template 必须是非空图片路径")
        if verify_template is not None and (
            not isinstance(verify_template, str) or not verify_template.strip()
        ):
            raise TemplateMatchError(f"{prefix}.verify_template 必须是非空图片路径或 null")
        if not isinstance(click, bool):
            raise TemplateMatchError(f"{prefix}.click 必须是 true 或 false")

        steps.append(
            {
                "name": name,
                # 相对路径以工作流文件所在目录为基准，调用命令时无需切换目录。
                "template": (
                    Path(template)
                    if Path(template).is_absolute()
                    else workflow_path.parent / Path(template)
                ),
                "verify_template": (
                    None
                    if verify_template is None
                    else (
                        Path(verify_template)
                        if Path(verify_template).is_absolute()
                        else workflow_path.parent / Path(verify_template)
                    )
                ),
                "threshold": validate_threshold(
                    raw_step.get("threshold", 0.85), f"{prefix}.threshold"
                ),
                "scales": validate_scale_list(
                    raw_step.get("scales", [1.0]), f"{prefix}.scales"
                ),
                "match_mode": validate_match_mode(
                    raw_step.get("match_mode", "edges"), f"{prefix}.match_mode"
                ),
                "click": click,
                "wait_after_click_seconds": validate_wait_after_click_seconds(
                    raw_step.get(
                        "wait_after_click_seconds", DEFAULT_WAIT_AFTER_CLICK_SECONDS
                    ),
                    f"{prefix}.wait_after_click_seconds",
                ),
            }
        )
    return workflow_name, steps


def run_match(
    cv2,
    *,
    template_path: Path,
    image_path: Path | None,
    region: tuple[int, int, int, int] | None,
    threshold: float,
    scales: Sequence[float],
    match_mode: str,
    output_path: Path | None,
    click: bool,
    wait_after_click_seconds: float = DEFAULT_WAIT_AFTER_CLICK_SECONDS,
    verify_template_path: Path | None = None,
) -> dict[str, object]:
    """执行单次模板匹配；点击前后均独立回读实时屏幕。"""
    template = read_image(cv2, template_path)
    screen, origin, match = locate_template(
        cv2, template, image_path, region, scales, match_mode, threshold
    )
    match["matched"] = match["score"] >= threshold

    if match["matched"] and output_path is not None:
        # 标注图的坐标需要相对于当前屏幕截图，而非全局屏幕坐标。
        local_match = dict(match)
        local_match["x"] -= origin[0]
        local_match["y"] -= origin[1]
        save_annotated_image(cv2, screen, local_match, output_path)

    if not match["matched"] or not click:
        return match

    # 点击前遵循 GUI 操作防护：等待后重新截屏并重新匹配当前目标。
    time.sleep(CLICK_REVERIFY_DELAY_SECONDS)
    _, _, confirmed_match = locate_template(
        cv2, template, None, region, scales, match_mode, threshold
    )
    confirmed_match["matched"] = confirmed_match["score"] >= threshold
    if not confirmed_match["matched"]:
        confirmed_match["clicked"] = False
        confirmed_match["reason"] = "重新截屏后未达到匹配阈值，未执行点击"
        return confirmed_match

    pyautogui = require_pyautogui()
    try:
        pyautogui.click(int(confirmed_match["click_x"]), int(confirmed_match["click_y"]))
    except Exception as exc:
        raise TemplateMatchError(
            "鼠标点击失败。请在 macOS“系统设置 → 隐私与安全性 → 辅助功能”中"
            "允许当前 Terminal 或 Python。"
        ) from exc
    confirmed_match["clicked"] = True

    # 点击后按节点配置等待并读取最新屏幕；截图失败时工作流不会进入下一步。
    time.sleep(wait_after_click_seconds)
    try:
        if verify_template_path is not None:
            verify_template = read_image(cv2, verify_template_path)
            _, _, next_match = locate_template(
                cv2,
                verify_template,
                None,
                region,
                scales,
                match_mode,
                threshold,
            )
            confirmed_match["next_template_score"] = next_match["score"]
            confirmed_match["next_template_verified"] = True
        else:
            capture_screen(region)
            confirmed_match["next_template_verified"] = None
        confirmed_match["post_click_screen_read"] = True
    except TemplateMatchError as exc:
        # 鼠标点击已经执行；不能确认现状时禁止工作流执行后续步骤。
        confirmed_match["post_click_screen_read"] = False
        confirmed_match["post_click_verification_error"] = str(exc)
    return confirmed_match


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 OpenCV 在屏幕或图片中匹配黑白模板")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--template", type=Path, help="作为匹配目标的 PNG/JPEG 图片")
    input_group.add_argument("--workflow", type=Path, help="按顺序执行模板点击的 JSON 工作流")
    parser.add_argument("--image", type=Path, help="待匹配图片；不提供时截取当前屏幕")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.60,
        help="判定匹配成功的最低得分，取值范围 0 到 1（默认：0.60；边缘模式适用）",
    )
    parser.add_argument(
        "--scales",
        type=parse_scales,
        default=[1.0],
        help="要尝试的模板缩放比例，逗号分隔（默认：1.0）",
    )
    parser.add_argument(
        "--match-mode",
        choices=MATCH_MODES,
        default="edges",
        help="黑白特征提取方式：edges（默认，抗悬停反色）或 binary",
    )
    parser.add_argument(
        "--region",
        type=parse_region,
        help="只截图该区域：x,y,width,height；坐标为屏幕坐标",
    )
    parser.add_argument("--output", type=Path, help="匹配成功时保存带绿色标记框的图片")
    parser.add_argument(
        "--click",
        action="store_true",
        help="匹配成功后重新确认一次，再点击模板中心点",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.threshold = validate_threshold(args.threshold, "--threshold")
    args.match_mode = validate_match_mode(args.match_mode, "--match-mode")
    if args.image is not None and args.region is not None:
        raise TemplateMatchError("--image 与 --region 不能同时使用")
    if args.click and args.image is not None:
        raise TemplateMatchError("--click 仅能用于实时屏幕匹配，不能与 --image 同时使用")
    if args.workflow is not None and args.image is not None:
        raise TemplateMatchError("--workflow 仅能用于实时屏幕匹配，不能与 --image 同时使用")
    if args.workflow is not None and args.output is not None:
        raise TemplateMatchError("--output 仅支持单模板模式，工作流会输出 JSON 结果")
    if args.workflow is not None and args.click:
        raise TemplateMatchError("工作流的每一步由 JSON 中的 click 字段控制，不要传入 --click")

    cv2 = require_cv2()
    if args.workflow is None:
        match = run_match(
            cv2,
            template_path=args.template,
            image_path=args.image,
            region=args.region,
            threshold=args.threshold,
            scales=args.scales,
            match_mode=args.match_mode,
            output_path=args.output,
            click=args.click,
        )
        print(json.dumps(match, ensure_ascii=False))
        return 0 if match["matched"] else 1

    workflow_name, steps = load_workflow(args.workflow)
    results: list[dict[str, object]] = []
    for step in steps:
        result = run_match(
            cv2,
            template_path=step["template"],
            image_path=None,
            region=args.region,
            threshold=step["threshold"],
            scales=step["scales"],
            match_mode=step["match_mode"],
            output_path=None,
            click=step["click"],
            wait_after_click_seconds=step["wait_after_click_seconds"],
            verify_template_path=step["verify_template"],
        )
        results.append({"name": step["name"], **result})
        if not result["matched"] or result.get("clicked") is False:
            print(json.dumps({"workflow": workflow_name, "steps": results}, ensure_ascii=False))
            return 1
        if step["click"] and not result.get("post_click_screen_read", False):
            print(json.dumps({"workflow": workflow_name, "steps": results}, ensure_ascii=False))
            return 1
        if step["verify_template"] is not None and not result.get(
            "next_template_verified", False
        ):
            print(json.dumps({"workflow": workflow_name, "steps": results}, ensure_ascii=False))
            return 1

    print(json.dumps({"workflow": workflow_name, "steps": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TemplateMatchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
