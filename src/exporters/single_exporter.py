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

        gui_utils.center_window(self, 500, 680)
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

    def _build_ui(self):
        main = ttk.Frame(self, padding='10')
        main.pack(fill='both', expand=True)

        self._build_output_section(main)
        self._build_mode_section(main)
        self._build_video_options(main)
        self._build_audio_options(main)
        self._build_progress_section(main)
        self._build_button_section(main, export_text='Export')
        self._update_widget_states()

    def _build_output_section(self, parent):
        frame = ttk.LabelFrame(parent, text='Output', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        self.output_path_var = tk.StringVar(self, value=self._generate_default_output())

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill='x')

        ttk.Entry(path_frame, textvariable=self.output_path_var).pack(
            side='left',
            fill='x',
            expand=True
        )
        ttk.Button(path_frame, text='Browse...', command=self._browse_output).pack(
            side='left',
            padx=(5, 0)
        )

    def _build_progress_section(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=(0, 10))

        self.progress_var = tk.DoubleVar(self, value=0.0)
        self.progress_bar = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', pady=(0, 5))

        self.status_var = tk.StringVar(self, value='Ready')
        ttk.Label(frame, textvariable=self.status_var).pack(anchor='w')

        self.keyframe_info_var = tk.StringVar(self, value=self._get_keyframe_info())
        self.keyframe_label = ttk.Label(frame, textvariable=self.keyframe_info_var, font=('', 8))
        self.keyframe_label.pack(anchor='w')

    def _generate_default_output(self) -> str:
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        start_sec = self.start_ms / 1000.0
        end_sec = self.end_ms / 1000.0

        return os.path.join(
            os.path.dirname(self.video_path),
            f'{base}_scene_{start_sec:.1f}s-{end_sec:.1f}s.mp4'
        )

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
        config.save_config(self.config)

    def _start_export(self):
        output_path = self.output_path_var.get()

        if not output_path:
            messagebox.showerror('Error', 'Please specify an output path.', parent=self)
            return

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
            self._run_ffmpeg(cmd)
        except Exception as e:
            self.after(0, lambda err=str(e): self._on_export_error(err))

    def _build_ffmpeg_command(self) -> list:
        cmd = [self._get_ffmpeg_path()]

        if self.mode_var.get() == 'copy':
            start_sec = self.metadata['keyframe_ms'] / 1000.0

            cmd.extend(['-ss', str(start_sec)])
            cmd.extend(['-i', self.video_path])
            cmd.extend(['-c', 'copy'])
        else:
            start_sec = self.start_ms / 1000.0

            buffer_sec = 10.0
            if start_sec > buffer_sec:
                fast_seek = start_sec - buffer_sec
                exact_seek = buffer_sec
            else:
                fast_seek = 0.0
                exact_seek = start_sec

            cmd.extend(['-ss', str(fast_seek)])
            cmd.extend(['-i', self.video_path])
            cmd.extend(['-ss', str(exact_seek)])
            cmd.extend(self._get_video_encode_args())

        cmd.extend(self._get_audio_args())

        cmd.extend(['-map', '0:v:0'])
        if self.metadata.get('has_audio'):
            cmd.extend(['-map', '0:a?'])

        duration_sec = self.duration_ms / 1000.0
        cmd.extend([
            '-t', str(duration_sec),
            '-avoid_negative_ts', 'make_zero',
            '-y',
            self.output_path_var.get()
        ])

        return cmd

    def _run_ffmpeg(self, cmd: list):
        creation_flags = 0
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            bufsize=1,
            universal_newlines=True
        )

        process = self.process
        time_regex = re.compile(r'time=(\d+:\d+:\d+\.\d+)')
        stderr_lines = []

        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line)

            if self.cancelled:
                process.terminate()
                process.wait()
                self.after(0, self._on_export_cancelled)
                return

            match = time_regex.search(line)
            if match:
                current_ms = self._parse_time_to_ms(match.group(1))
                progress = min(100.0, (current_ms / self.duration_ms) * 100.0) if self.duration_ms else 0.0
                status = (
                    f'Exporting... {self._format_ms(current_ms)} / '
                    f'{self._format_ms(self.duration_ms)}'
                )
                self.after(0, lambda p=progress, s=status: self._update_progress(p, s))

        process.wait()

        if process.returncode == 0 and not self.cancelled:
            self.after(0, self._on_export_complete)
        elif not self.cancelled:
            stderr_output = ''.join(stderr_lines[-80:])
            self.after(
                0,
                lambda err=f'FFmpeg exited with code {process.returncode}\n\n{stderr_output}':
                    self._on_export_error(err)
            )

    def _update_progress(self, progress: float, status: str):
        self.progress_var.set(progress)
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
            output_abs = os.path.abspath(output_path)
            folder = os.path.dirname(output_abs)

            try:
                if sys.platform == 'win32':
                    subprocess.run(['explorer', '/select,', output_abs])
                elif sys.platform == 'darwin':
                    subprocess.run(['open', '-R', output_abs])
                else:
                    subprocess.run(['xdg-open', folder])
            except Exception as e:
                print(f'Failed to open output directory: {e}')

        self.destroy()

    def _on_export_error(self, error_msg: str):
        self.status_var.set('Export failed!')
        messagebox.showerror('Export Error', error_msg, parent=self)
        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_export_cancelled(self):
        self.status_var.set('Export cancelled.')

        output_path = self.output_path_var.get()
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass

        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_cancel(self):
        if self.process and self.process.poll() is None:
            self.cancelled = True
            self.status_var.set('Cancelling...')
            self.cancel_btn.config(state='disabled')
        else:
            self.destroy()
