"""PixPack 可视化界面。打包成 PNG，或从 PNG 还原，并显示进度。"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pixpack import (
    PixpackError,
    default_max_side,
    format_preview,
    pack_directory,
    preview_pack,
    side_choices,
    side_limit_hint,
    unpack_directory,
)


def _ui_font() -> str:
    if sys.platform == "darwin":
        return "PingFang SC"
    if sys.platform == "win32":
        return "Microsoft YaHei UI"
    return "Sans"


def _mono_font() -> str:
    if sys.platform == "darwin":
        return "Menlo"
    if sys.platform == "win32":
        return "Consolas"
    return "Monospace"


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("xai.pixpack.gui")
    except Exception:
        pass


class PixPackApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PixPack 图片打包工具")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.configure(bg="#f3f5f8")

        self.queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.started_at = 0.0
        self._pulse = 0
        self.last_output: Path | None = None

        self.pack_src = tk.StringVar()
        self.pack_out = tk.StringVar()
        self.max_side = tk.StringVar(value=str(default_max_side()))
        self.parts = tk.StringVar()
        self.speed = tk.StringVar(value="极速（只打包）")
        self.prefix = tk.StringVar(value="pixpack")
        self.unpack_images = tk.StringVar()
        self.unpack_out = tk.StringVar()
        self.percent = tk.StringVar(value="0%")
        self.status = tk.StringVar(value="准备就绪")

        self._build()
        self.after(80, self._poll)

    def _build(self) -> None:
        style = ttk.Style(self)
        if sys.platform == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        elif sys.platform == "darwin" and "aqua" in style.theme_names():
            style.theme_use("aqua")
        ui_font = _ui_font()
        style.configure("TLabel", font=(ui_font, 10))
        style.configure("TButton", font=(ui_font, 10))
        style.configure("TLabelframe.Label", font=(ui_font, 10, "bold"))
        style.configure("Header.TLabel", font=(ui_font, 18, "bold"))
        style.configure("Hint.TLabel", font=(ui_font, 9), foreground="#667085")
        style.configure("Percent.TLabel", font=(ui_font, 22, "bold"))
        style.configure("Run.TButton", font=(ui_font, 11, "bold"))

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="PixPack", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="把文件夹无损打包成一张或多张 PNG，再从这些图片还原。不要把 PNG 转成 JPEG 或缩放。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="x")
        self.notebook.add(self._pack_tab(), text="  打包成图片  ")
        self.notebook.add(self._unpack_tab(), text="  从图片还原  ")

        progress_box = ttk.LabelFrame(outer, text="进度", padding=12)
        progress_box.pack(fill="x", pady=(12, 8))
        head = ttk.Frame(progress_box)
        head.pack(fill="x")
        ttk.Label(head, textvariable=self.percent, style="Percent.TLabel").pack(side="left")
        self.status_label = ttk.Label(progress_box, textvariable=self.status, wraplength=680, justify="left")
        self.status_label.pack(fill="x", pady=(6, 0))
        self.bar = ttk.Progressbar(progress_box, maximum=1000, mode="determinate")
        self.bar.pack(fill="x", pady=(8, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(4, 8))
        self.start_button = ttk.Button(buttons, text="开始", style="Run.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.preview_button = ttk.Button(buttons, text="预览规模", command=self._preview)
        self.preview_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(buttons, text="打开输出目录", command=self._open_output, state="disabled")
        self.open_button.pack(side="left", padx=(8, 0))

        log_box = ttk.LabelFrame(outer, text="日志", padding=8)
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_box,
            height=12,
            wrap="word",
            font=(_mono_font(), 10),
            bg="#ffffff",
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _pack_tab(self) -> ttk.Frame:
        frame = ttk.Frame(self.notebook, padding=12)
        ttk.Label(frame, text="源").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.pack_src).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        src_picks = ttk.Frame(frame)
        src_picks.grid(row=0, column=2, sticky="e")
        ttk.Button(src_picks, text="选择文件夹", command=self._browse_pack_src).pack(side="left")
        ttk.Button(src_picks, text="选择文件", command=self._browse_pack_file).pack(side="left", padx=(6, 0))
        self._path_row(frame, 1, "输出文件夹", self.pack_out, self._browse_pack_out)

        options = ttk.Frame(frame)
        options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(options, text="最大边长").pack(side="left")
        ttk.Combobox(
            options,
            textvariable=self.max_side,
            width=8,
            state="readonly",
            values=tuple(str(item) for item in side_choices()),
        ).pack(side="left", padx=(6, 16))
        ttk.Label(options, text="张数（留空自动）").pack(side="left")
        ttk.Entry(options, textvariable=self.parts, width=6).pack(side="left", padx=(6, 16))
        ttk.Label(options, text="速度").pack(side="left")
        ttk.Combobox(
            options,
            textvariable=self.speed,
            width=16,
            state="readonly",
            values=("极速（只打包）", "快速（推荐）", "标准", "高压缩（很慢）"),
        ).pack(side="left", padx=(6, 16))
        ttk.Label(options, text="前缀").pack(side="left")
        ttk.Entry(options, textvariable=self.prefix, width=12).pack(side="left", padx=(6, 0))
        ttk.Label(
            frame,
            text=(
                "源可以是一个文件夹，也可以是单个文件。默认「极速」适合大量文件。"
                + side_limit_hint()
            ),
            style="Hint.TLabel",
            wraplength=680,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        return frame

    def _unpack_tab(self) -> ttk.Frame:
        frame = ttk.Frame(self.notebook, padding=12)
        ttk.Label(frame, text="图片").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.unpack_images).grid(row=0, column=1, sticky="ew", padx=8)
        picks = ttk.Frame(frame)
        picks.grid(row=0, column=2, sticky="e")
        ttk.Button(picks, text="选择目录", command=self._browse_unpack_dir).pack(side="left")
        ttk.Button(picks, text="选择文件", command=self._browse_unpack_files).pack(side="left", padx=(6, 0))
        self._path_row(frame, 1, "还原到", self.unpack_out, lambda: self._choose_dir(self.unpack_out))
        ttk.Label(
            frame,
            text="只需要选一个文件夹。图片里的标记会决定：单个文件按原文件名写出，文件夹按原来的路径展开。",
            style="Hint.TLabel",
            wraplength=680,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        frame.columnconfigure(1, weight=1)
        return frame

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(parent, text="浏览…", command=command).grid(row=row, column=2, pady=4)

    def _browse_pack_src(self) -> None:
        self._choose_dir(self.pack_src)

    def _browse_pack_file(self) -> None:
        current = self.pack_src.get().strip()
        initial = str(Path(current).parent) if current and Path(current).parent.is_dir() else None
        path = filedialog.askopenfilename(title="选择要打包的文件", initialdir=initial)
        if path:
            self.pack_src.set(path)

    def _browse_pack_out(self) -> None:
        self._choose_dir(self.pack_out)

    def _browse_unpack_dir(self) -> None:
        self._choose_dir(self.unpack_images)

    def _browse_unpack_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 pixpack PNG",
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")],
        )
        if paths:
            self.unpack_images.set("|".join(paths))

    def _choose_dir(self, variable: tk.StringVar) -> None:
        current = variable.get().strip()
        initial = current if current and Path(current).is_dir() else None
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            variable.set(path)

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.preview_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.notebook.state(["disabled"] if running else ["!disabled"])

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        packing = self.notebook.index(self.notebook.select()) == 0
        try:
            job = self._prepare_pack() if packing else self._prepare_unpack()
        except PixpackError as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        self.cancel_event.clear()
        self.started_at = time.monotonic()
        self.bar["value"] = 0
        self.percent.set("0%")
        self.status.set("正在启动…")
        self._set_running(True)
        self._append_log("—— 开始" + ("打包" if packing else "还原") + " ——")
        self.worker = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        self.worker.start()

    def _prepare_pack(self):
        src = self.pack_src.get().strip()
        out = self.pack_out.get().strip()
        if not src or not out:
            raise PixpackError("请选择源文件或文件夹，以及输出文件夹")
        try:
            max_side = int(self.max_side.get().strip())
        except ValueError as exc:
            raise PixpackError("最大边长必须是整数") from exc
        level, store_compressed = {
            "极速（只打包）": (0, True),
            "快速（推荐）": (1, True),
            "标准": (6, True),
            "高压缩（很慢）": (9, False),
        }.get(self.speed.get(), (1, True))
        parts_text = self.parts.get().strip()
        parts = None
        if parts_text:
            try:
                parts = int(parts_text)
            except ValueError as exc:
                raise PixpackError("张数必须是整数，或留空表示自动") from exc
            if parts < 1:
                raise PixpackError("张数至少为 1")
        self.last_output = Path(out)
        return (
            "pack",
            Path(src),
            Path(out),
            max_side,
            parts,
            level,
            store_compressed,
            self.prefix.get().strip() or "pixpack",
        )

    def _preview(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.notebook.index(self.notebook.select()) != 0:
            messagebox.showinfo("预览", "预览只看「打包成图片」这一页的设置。")
            return
        try:
            job = self._prepare_preview()
        except PixpackError as exc:
            messagebox.showerror("无法预览", str(exc))
            return
        self.cancel_event.clear()
        self.started_at = time.monotonic()
        self.bar["value"] = 0
        self.percent.set("…")
        self.status.set("正在预览…")
        self._set_running(True)
        self._append_log("—— 预览规模 ——")
        self.worker = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        self.worker.start()

    def _prepare_preview(self):
        src = self.pack_src.get().strip()
        if not src:
            raise PixpackError("请先选择源文件或文件夹")
        try:
            max_side = int(self.max_side.get().strip())
        except ValueError as exc:
            raise PixpackError("最大边长必须是整数") from exc
        level, store_compressed = {
            "极速（只打包）": (0, True),
            "快速（推荐）": (1, True),
            "标准": (6, True),
            "高压缩（很慢）": (9, False),
        }.get(self.speed.get(), (0, True))
        parts_text = self.parts.get().strip()
        parts = None
        if parts_text:
            try:
                parts = int(parts_text)
            except ValueError as exc:
                raise PixpackError("张数必须是整数，或留空表示自动") from exc
        out = self.pack_out.get().strip()
        return (
            "preview",
            Path(src),
            Path(out) if out else None,
            max_side,
            parts,
            level,
            store_compressed,
            self.speed.get(),
        )

    def _prepare_unpack(self):
        raw = self.unpack_images.get().strip()
        out = self.unpack_out.get().strip()
        if not raw or not out:
            raise PixpackError("请选择图片和还原目录")
        images = [Path(part) for part in raw.split("|") if part.strip()]
        if not images:
            raise PixpackError("请选择图片")
        self.last_output = Path(out)
        return ("unpack", images, Path(out))

    def _run_job(self, job) -> None:
        state = {"t": 0.0, "f": -1.0}

        def progress(frac: float, message: str) -> None:
            now = time.monotonic()
            if frac < 1 and now - state["t"] < 0.08 and abs(frac - state["f"]) < 0.004:
                return
            state["t"] = now
            state["f"] = frac
            self.queue.put(("progress", frac, message))

        def log(message: str) -> None:
            self.queue.put(("log", message))

        try:
            if job[0] == "preview":
                _, src, out, max_side, parts, level, store_compressed, mode_label = job
                result = preview_pack(
                    src,
                    out_dir=out,
                    max_side=max_side,
                    parts=parts,
                    level=level,
                    store_compressed=store_compressed,
                    mode_label=mode_label,
                    progress=progress,
                    cancel=self.cancel_event.is_set,
                )
                text = format_preview(result)
                self.queue.put(("log", text))
                self.queue.put(("preview", text, result.error is None))
                return
            if job[0] == "pack":
                _, src, out, max_side, parts, level, store_compressed, prefix = job
                file_count, images = pack_directory(
                    src,
                    out,
                    max_side=max_side,
                    parts=parts,
                    level=level,
                    prefix=prefix,
                    store_compressed=store_compressed,
                    progress=progress,
                    cancel=self.cancel_event.is_set,
                    log=log,
                )
                summary = f"完成：{file_count} 个文件打包成 {len(images)} 张 PNG\n{out}"
            else:
                _, images, out = job
                frame_count, file_count = unpack_directory(
                    images,
                    out,
                    progress=progress,
                    cancel=self.cancel_event.is_set,
                    log=log,
                )
                summary = f"完成：从 {frame_count} 张图片还原 {file_count} 个文件\n{out}"
            self.queue.put(("progress", 1.0, "完成"))
            self.queue.put(("done", summary))
        except PixpackError as exc:
            self.queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - 显示到界面，避免窗口线程直接崩掉
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            self.queue.put(("idle",))

    def _show_preview(self, text: str, can_start: bool) -> None:
        window = tk.Toplevel(self)
        window.title("打包预览")
        window.geometry("680x480")
        window.transient(self)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="按当前设置，大概会生成这些图片。还没有开始写文件。").pack(anchor="w")
        box = tk.Text(frame, wrap="word", font=(_ui_font(), 10), height=16)
        box.pack(fill="both", expand=True, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")
        actions = ttk.Frame(frame)
        actions.pack(fill="x")

        def start_from_preview() -> None:
            window.destroy()
            self._start()

        if can_start:
            ttk.Button(actions, text="按此配置开始", command=start_from_preview).pack(side="left")
        ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")

    def _cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status.set("正在取消…")
            self._append_log("正在取消，当前这一小段处理完后会停止。")

    def _open_output(self) -> None:
        if self.last_output is None:
            return
        path = self.last_output
        if not path.exists():
            messagebox.showinfo("提示", f"目录还不存在：\n{path}")
            return
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - 用户主动打开自己选的输出目录
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)

    def _poll(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    frac = float(item[1])
                    elapsed = int(time.monotonic() - self.started_at) if self.started_at else 0
                    if frac < 0:
                        # 总量还未知。不要改成 indeterminate：vista 主题里
                        # start/stop 一切换，进程会直接消失，连错误框都没有。
                        self._pulse = (self._pulse + 40) % 1000
                        self.bar["value"] = self._pulse
                        self.percent.set("…")
                    else:
                        self.bar["value"] = int(max(0.0, min(1.0, frac)) * 1000)
                        self.percent.set(f"{frac * 100:.0f}%")
                    self.status.set(f"{item[2]}    已用 {elapsed} 秒")
                elif kind == "log":
                    self._append_log(str(item[1]))
                elif kind == "preview":
                    self._show_preview(str(item[1]), bool(item[2]))
                elif kind == "done":
                    self._append_log(str(item[1]))
                    self.open_button.configure(state="normal")
                    messagebox.showinfo("完成", str(item[1]))
                elif kind == "error":
                    text = str(item[1])
                    self._append_log("错误: " + text)
                    if text == "已取消":
                        self.status.set("已取消")
                        messagebox.showinfo("已取消", "操作已取消。已经写出的文件会保留。")
                    else:
                        self.status.set("失败")
                        messagebox.showerror("失败", text)
                elif kind == "idle":
                    self._set_running(False)
        except queue.Empty:
            pass
        except Exception as exc:
            self._report_ui_error(exc)
        self.after(80, self._poll)

    def report_callback_exception(self, exc, val, tb) -> None:  # noqa: ARG002
        # 打包后的窗口程序没有控制台，sys.stderr 是 None。
        # Tk 默认把回调异常打到 stderr，这一下又会把整个窗口直接关掉。
        self._report_ui_error(val)

    def _report_ui_error(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}"
        try:
            self._append_log("界面错误: " + text)
            self.status.set("失败")
            self._set_running(False)
        except Exception:
            pass
        try:
            messagebox.showerror("PixPack", text)
        except Exception:
            pass


def main() -> None:
    _enable_windows_dpi()
    app = PixPackApp()
    app.mainloop()


if __name__ == "__main__":
    main()
