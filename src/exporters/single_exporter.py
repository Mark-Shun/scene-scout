import os
import re
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import config
import gui_utils

from .base_exporter import BaseExporter, get_video_info_and_keyframe, export_video_scene


class SingleExportDialog(BaseExporter):
    def __init__(self, parent, video_path: str, start_ms: int, end_ms: int):
        super().__init__(parent)

        self.video_path = video_path
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.duration_ms = end_ms - start_ms
        self.metadata = get_video_info_and_keyframe(self.video_path, self.start_ms)

        self.title('Export Scene')
        self._build_ui()

        gui_utils.center_window(self, 500, 750)
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

    def _build_ui(self):
        main = self._setup_scrollable_container()

        self._build_container_section(main)
        self._build_mode_section(main)
        self._build_naming_section(main, is_bulk=False)
        self._build_video_options(main)
        self._build_audio_options(main)
        self._build_progress_section(main)
        self._build_button_section(main, export_text='Export')
        self._update_widget_states()
        self._update_preview_display()

    def _get_preview_params(self):
        return self.metadata, self.video_path, self.start_ms, self.end_ms

    def _build_progress_section(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=(0, 10))

        self.progress_bar = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', pady=(0, 5))

        ttk.Label(frame, textvariable=self.status_var).pack(anchor='w')

        self.keyframe_info_var = tk.StringVar(self, value=self._get_keyframe_info())
        self.keyframe_label = ttk.Label(frame, textvariable=self.keyframe_info_var, font=('', 8))
        self.keyframe_label.pack(anchor='w')

    def _browse_output(self):
        initial = self.output_path_var.get()
        path = filedialog.asksaveasfilename(
            title='Export Scene As',
            initialfile=os.path.basename(initial),
            initialdir=os.path.dirname(initial),
            defaultextension='.mp4',
            filetypes=[
                ('MP4 Video', '*.mp4'),
                ('Matroska Video', '*.mkv'),
                ('WebM Video', '*.webm'),
                ('All Files', '*.*')
            ]
        )

        if path:
            self.output_path_var.set(path)

    def _get_keyframe_info(self) -> str:
        if self.mode_var.get() == 'copy':
            return (
                f"Note: Stream Copy snaps to keyframe at "
                f"{self._format_ms(self.metadata['keyframe_ms'])}, timing may not be exact"
            )

        return f'Exact frame accuracy: {self._format_ms(self.start_ms)}'

    def _save_settings(self):
        self._save_common_settings()
        self.config['naming_template'] = self.template_var.get()
        config.save_config(self.config)

    def _start_export(self):
        output_path = self.output_path_var.get()

        if not output_path:
            messagebox.showerror('Error', 'Please specify an output path.', parent=self)
            return

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if os.path.exists(output_path):
            if not messagebox.askyesno(
                'Overwrite?',
                f'{os.path.basename(output_path)} already exists. Overwrite?',
                parent=self
            ):
                return

        self._save_settings()

        self.export_btn.config(state='disabled')
        self.cancel_btn.config(text='Cancel', state='normal')
        self.cancelled = False

        self.progress_var.set(0)
        self.status_var.set('Starting export...')

        self.export_thread = threading.Thread(target=self._export_task, daemon=True)
        self.export_thread.start()

        self.after(100, self._check_export_progress)

    def _export_task(self):
        try:
            cmd = self._build_ffmpeg_command()
            result = self._run_ffmpeg_base(cmd, self.duration_ms, self._update_progress)

            if result == "success":
                self.after(0, self._on_export_complete)
            elif result != "cancelled":
                self.after(0, lambda e=result: self._on_export_error(e))
        except Exception as e:
            self.after(0, lambda err=str(e): self._on_export_error(err))

    def _build_ffmpeg_command(self) -> list:
        cmd = [self._get_ffmpeg_path()]

        if self.mode_var.get() == 'copy':
            # Stream copy: Fast seek directly to the keyframe
            start_sec = self.metadata['keyframe_ms'] / 1000.0
            cmd.extend(['-ss', str(start_sec), '-i', self.video_path, '-c', 'copy'])
        else:
            # Re-encode: Fast seek followed by accurate decode
            start_sec = self.start_ms / 1000.0
            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek

            cmd.extend(['-ss', str(fast_seek), '-i', self.video_path, '-ss', str(exact_seek)])
            
            # Inject core encoding arguments from the base class
            cmd.extend(self._get_core_ffmpeg_args(self.metadata))

        # Add output-specific parameters
        duration_sec = self.duration_ms / 1000.0
        cmd.extend(['-t', str(duration_sec), self.output_path_var.get()])

        return cmd

    def _update_progress(self, _current_ms: int, percent: float):
        status = f'Exporting... {self._format_ms(_current_ms)} / {self._format_ms(self.duration_ms)}'
        self.progress_var.set(percent)
        self.status_var.set(status)

    def _check_export_progress(self):
        if self.export_thread and self.export_thread.is_alive():
            self.after(100, self._check_export_progress)
        else:
            self.export_btn.config(state='normal')

    def _on_export_complete(self):
        self.progress_var.set(100)
        self.status_var.set('Export complete!')

        output_path = self.output_path_var.get()

        messagebox.showinfo(
            'Success',
            f'Scene exported successfully to:\n{output_path}',
            parent=self
        )

        if self.open_folder_var.get():
            self._open_target_explorer(output_path)

        self.destroy()

    def _on_cancel(self):
        self.cancelled = True
        if self.process:
            self.process.terminate()
        self.destroy()
