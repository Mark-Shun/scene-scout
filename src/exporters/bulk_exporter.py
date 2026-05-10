import os
import re
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional

import config
import gui_utils

from .base_exporter import BaseExporter, get_video_info_and_keyframe


class BulkExportDialog(BaseExporter):
    def __init__(self, parent, scenes: list):
        super().__init__(parent)

        self.failed_exports = []

        self.scenes = scenes
        self.current_output_path: Optional[str] = None
        self.completed_outputs: list[str] = []
        self.current_scene_idx = 0
        self.current_scene_total = len(scenes)
        self.metadata_by_scene: list[Dict[str, Any]] = []
        self.planned_outputs: list[str] = []

        self._init_naming_vars()
        self.output_dir_var.set(self._generate_default_output_dir())
        self.title(f'Bulk Export — {len(scenes)} Scene(s)')
        self._build_ui()

        self.export_btn.config(state='disabled')
        self._start_metadata_analysis() # Threaded analysis

        gui_utils.center_window(self, 540, 960)
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

    def _extract_metadata(self):
        self.metadata_by_scene = []

        for video_path, start_ms, _end_ms in self.scenes:
            self.metadata_by_scene.append(
                get_video_info_and_keyframe(video_path, start_ms)
            )

    def _build_ui(self):
        main = ttk.Frame(self, padding='10')
        main.pack(fill='both', expand=True)

        self._build_container_section(main)
        self._build_mode_section(main)
        self._build_naming_section(main, is_bulk=True)
        self._build_video_options(main)
        self._build_audio_options(main)
        self._build_progress_section(main)
        self._build_button_section(main, export_text='Export All')
        self._update_widget_states()

    def _get_preview_params(self):
        v_path, s_ms, e_ms = self.scenes[0] if self.scenes else ('video.mp4', 0, 10000)
        meta = self.metadata_by_scene[0] if self.metadata_by_scene else {}
        return meta, v_path, s_ms, e_ms

    def _build_container_section(self, parent):
        frame = ttk.LabelFrame(parent, text='Container', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        ttk.Label(frame, text='Format:').grid(row=0, column=0, sticky='w', pady=2)

        self.container_combo = ttk.Combobox(
            frame,
            textvariable=self.container_var,
            values=list(self.CONTAINERS.keys()),
            state='readonly',
            width=20
        )
        self.container_combo.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=2)
        self.container_combo.bind('<<ComboboxSelected>>', lambda _e: self._update_preview_display())

    def _start_metadata_analysis(self):
        """Initializes the background thread for scene analysis."""
        self.status_var.set("Analyzing video files...")
        thread = threading.Thread(target=self._threaded_metadata_task, daemon=True)
        thread.start()

    def _threaded_metadata_task(self):
        """Background task to extract metadata for all scenes."""
        temp_metadata = []
        total = len(self.scenes)
        
        for i, (video_path, start_ms, _end_ms) in enumerate(self.scenes):
            if self.cancelled:
                return

            # Update status on main thread
            msg = f"Analyzing scene {i+1}/{total}: {os.path.basename(video_path)}"
            self.after(0, lambda m=msg: self.status_var.set(m))
            
            # Perform the heavy lifting
            meta = get_video_info_and_keyframe(video_path, start_ms)
            temp_metadata.append(meta)

        # Finalize on the main thread
        self.after(0, lambda: self._on_metadata_finished(temp_metadata))

    def _on_metadata_finished(self, metadata_list):
        """Callback when analysis is complete to re-enable the UI."""
        self.metadata_by_scene = metadata_list
        self.export_btn.config(state='normal')
        self.status_var.set("Ready to export")
        # Refresh the keyframe info for the first scene
        self.keyframe_info_var.set(self._get_keyframe_info())

    def _build_progress_section(self, parent):
        frame = ttk.LabelFrame(parent, text='Progress', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        self.scene_label_var = tk.StringVar(
            self,
            value=f'Ready to export {len(self.scenes)} scene(s)'
        )
        ttk.Label(frame, textvariable=self.scene_label_var, font=('', 10, 'bold')).pack(
            anchor='w',
            pady=(0, 5)
        )

        ttk.Label(frame, text='Current scene:').pack(anchor='w')
        self.scene_progress_var = tk.DoubleVar(self, value=0.0)
        ttk.Progressbar(
            frame,
            variable=self.scene_progress_var,
            maximum=100
        ).pack(fill='x', pady=(2, 8))

        ttk.Label(frame, text='Overall:').pack(anchor='w')
        self.overall_progress_var = tk.DoubleVar(self, value=0.0)
        ttk.Progressbar(
            frame,
            variable=self.overall_progress_var,
            maximum=100
        ).pack(fill='x', pady=(2, 8))

        self.status_var = tk.StringVar(self, value='Ready')
        ttk.Label(frame, textvariable=self.status_var, wraplength=500).pack(anchor='w')

        self.keyframe_info_var = tk.StringVar(self, value=self._get_keyframe_info())
        self.keyframe_label = ttk.Label(
            frame,
            textvariable=self.keyframe_info_var,
            font=('', 8),
            wraplength=500
        )
        self.keyframe_label.pack(anchor='w', pady=(3, 0))

    def _generate_default_output_dir(self) -> str:
        if not self.scenes:
            return os.getcwd()

        return os.path.dirname(self.scenes[0][0])

    def _generate_default_output_path(self, video_path: str, start_ms: int, end_ms: int, scene_idx: int = 0) -> str:
        metadata = self.metadata_by_scene[scene_idx] if scene_idx < len(self.metadata_by_scene) else {}
        template = self.template_var.get()
        filename = self._resolve_naming_template(template, metadata, video_path, start_ms, end_ms, scene_idx)
        ext = self.CONTAINERS.get(self.container_var.get(), '.mp4')
        return os.path.join(self.output_dir_var.get(), f"{filename}{ext}")

    def _make_unique_output_path(self, output_path: str, reserved_paths: Optional[set[str]] = None) -> str:
        reserved_paths = reserved_paths or set()

        if output_path not in reserved_paths and not os.path.exists(output_path):
            return output_path

        folder = os.path.dirname(output_path)
        stem, ext = os.path.splitext(os.path.basename(output_path))
        counter = 2

        while True:
            candidate = os.path.join(folder, f'{stem}_{counter}{ext}')
            if candidate not in reserved_paths and not os.path.exists(candidate):
                return candidate
            counter += 1

    def _browse_output_dir(self):
        path = filedialog.askdirectory(
            title='Choose Bulk Export Folder',
            initialdir=self.output_dir_var.get() or os.getcwd()
        )

        if path:
            self.output_dir_var.set(path)

    def _get_keyframe_info(self) -> str:
        if not self.scenes:
            return ''

        _video_path, start_ms, _end_ms = self.scenes[self.current_scene_idx]
        metadata = (
            self.metadata_by_scene[self.current_scene_idx]
            if self.current_scene_idx < len(self.metadata_by_scene)
            else {}
        )

        if self.mode_var.get() == 'copy':
            keyframe_ms = metadata.get('keyframe_ms', start_ms)
            return (
                f'Note: Stream Copy snaps each scene to its nearest keyframe. '
                f'Current scene keyframe: {self._format_ms(keyframe_ms)}'
            )

        return f'Exact frame accuracy for current scene: {self._format_ms(start_ms)}'

    def _save_settings(self):
        self._save_common_settings()
        self.config['export_container'] = self.container_var.get()
        self.config['naming_template'] = self.template_var.get()
        config.save_config(self.config)

    def _start_export(self):
        output_dir = self.output_dir_var.get()

        if not output_dir:
            messagebox.showerror('Error', 'Please specify an output folder.', parent=self)
            return

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror(
                'Error',
                f'Could not create output folder:\n{output_dir}\n\n{e}',
                parent=self
            )
            return

        if not os.path.isdir(output_dir):
            messagebox.showerror('Error', 'Output path must be a folder.', parent=self)
            return

        if not self.scenes:
            messagebox.showerror('Error', 'No scenes selected for export.', parent=self)
            return

        planned_outputs = []
        reserved_paths = set()

        for idx, (video_path, start_ms, end_ms) in enumerate(self.scenes):
            output_path = self._make_unique_output_path(
                self._generate_default_output_path(video_path, start_ms, end_ms, idx),
                reserved_paths
            )
            planned_outputs.append(output_path)
            reserved_paths.add(output_path)

        existing_outputs = [p for p in planned_outputs if os.path.exists(p)]
        if existing_outputs:
            shown = '\n'.join(os.path.basename(p) for p in existing_outputs[:8])
            if len(existing_outputs) > 8:
                shown += f'\n...and {len(existing_outputs) - 8} more'

            if not messagebox.askyesno(
                'Overwrite?',
                f'{len(existing_outputs)} output file(s) already exist:\n\n{shown}\n\nOverwrite them?',
                parent=self
            ):
                return

        self.planned_outputs = planned_outputs
        self.completed_outputs = []
        self.current_output_path = None

        self._save_settings()

        self.export_btn.config(state='disabled')
        self.cancel_btn.config(text='Cancel', state='normal')
        self.cancelled = False

        self.scene_progress_var.set(0)
        self.overall_progress_var.set(0)
        self.status_var.set('Starting bulk export...')

        self.export_thread = threading.Thread(target=self._export_task, daemon=True)
        self.export_thread.start()

        self.after(100, self._check_export_progress)

    def _export_task(self):
        total = len(self.scenes)
        self.failed_exports = [] # Reset for new run

        for idx, (video_path, start_ms, end_ms) in enumerate(self.scenes):
            if self.cancelled:
                self.after(0, self._on_export_cancelled)
                return

            self.current_scene_idx = idx
            output_path = self.planned_outputs[idx]
            self.current_output_path = output_path

            # Update UI labels for the current scene
            self.after(0, lambda i=idx, t=total, vp=video_path: 
                    self.scene_label_var.set(f'Exporting {i+1}/{total}: {os.path.basename(vp)}'))
            
            try:
                # Ensure sub-folders exist for this scene
                try:
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                except Exception as folder_err:
                    raise RuntimeError(f"Failed to create output folder: {folder_err}")

                cmd = self._build_ffmpeg_command(idx, video_path, start_ms, end_ms, output_path)
                self._run_ffmpeg(cmd, idx, total, end_ms - start_ms)
                
                # If successful, add to completion list
                self.completed_outputs.append(output_path)
                
            except Exception as e:
                # Log the error and continue to the next scene
                error_msg = str(e)
                print(f"Error exporting scene {idx + 1}: {error_msg}")
                self.failed_exports.append((idx, error_msg))
                continue # Do continue the process, but give feedback about the failed export

        # Once the loop finishes, call the completion handler
        self.after(0, self._on_export_complete)

    def _build_ffmpeg_command(self, scene_idx: int, video_path: str, start_ms: int, end_ms: int, output_path: str) -> list:
        duration_sec = (end_ms - start_ms) / 1000.0
        metadata = self.metadata_by_scene[scene_idx] if scene_idx < len(self.metadata_by_scene) else {}

        cmd = [self._get_ffmpeg_path()]

        if self.mode_var.get() == 'copy':
            # Bulk Copy: Use pre-calculated metadata keyframe
            keyframe_ms = metadata.get('keyframe_ms', start_ms)
            cmd.extend(['-ss', str(keyframe_ms / 1000.0), '-i', video_path, '-c', 'copy'])
        else:
            # Bulk Re-encode: Two-step seek
            start_sec = start_ms / 1000.0
            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek

            cmd.extend(['-ss', str(fast_seek), '-i', video_path, '-ss', str(exact_seek)])
            
            # Inject shared core arguments
            cmd.extend(self._get_core_ffmpeg_args(metadata))

        # Add bulk-specific output path
        cmd.extend(['-t', str(duration_sec), output_path])

        return cmd

    def _run_ffmpeg(self, cmd: list, scene_idx: int, total_scenes: int, duration_ms: int):
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
                return

            match = time_regex.search(line)
            if match:
                current_ms = self._parse_time_to_ms(match.group(1))
                scene_progress = min(100.0, (current_ms / duration_ms) * 100.0) if duration_ms else 0.0
                overall_progress = ((scene_idx + (scene_progress / 100.0)) / total_scenes) * 100.0
                status = (
                    f'Exporting... {self._format_ms(current_ms)} / '
                    f'{self._format_ms(duration_ms)}'
                )

                self.after(
                    0,
                    lambda sp=scene_progress, op=overall_progress, s=status:
                        self._update_progress(sp, op, s)
                )

        process.wait()

        if process.returncode != 0 and not self.cancelled:
            stderr_output = ''.join(stderr_lines[-80:])
            raise RuntimeError(
                f'FFmpeg exited with code {process.returncode}\n\n{stderr_output}'
            )

        if not self.cancelled:
            completed_overall = ((scene_idx + 1) / total_scenes) * 100.0
            self.after(
                0,
                lambda op=completed_overall:
                    self._update_progress(100.0, op, 'Scene complete.')
            )

    def _update_progress(self, scene_progress: float, overall_progress: float, status: str):
        self.scene_progress_var.set(scene_progress)
        self.overall_progress_var.set(overall_progress)
        self.status_var.set(status)

    def _check_export_progress(self):
        if self.export_thread and self.export_thread.is_alive():
            self.after(100, self._check_export_progress)
        else:
            self.export_btn.config(state='normal')

    def _on_export_complete(self):
        success_count = len(self.completed_outputs)
        fail_count = len(self.failed_exports)
        
        self.scene_progress_var.set(100)
        self.overall_progress_var.set(100)
        
        if fail_count == 0:
            self.status_var.set('Bulk export complete!')
            msg = f'Successfully exported all {success_count} scene(s).'
        else:
            self.status_var.set(f'Export finished with {fail_count} failure(s).')
            msg = f'Exported {success_count} scene(s).\n{fail_count} scene(s) failed to export.'

        output_dir = os.path.abspath(self.output_dir_var.get())
        messagebox.showinfo('Bulk Export Results', f'{msg}\n\nFolder: {output_dir}', parent=self)

        if self.open_folder_var.get():
            try:
                if sys.platform == 'win32':
                    subprocess.run(['explorer', output_dir])
                elif sys.platform == 'darwin':
                    subprocess.run(['open', output_dir])
                else:
                    subprocess.run(['xdg-open', output_dir])
            except Exception as e:
                print(f'Failed to open output directory: {e}')

        self.destroy()

    def _on_export_error(self, error_msg: str):
        self.status_var.set('Bulk export failed!')
        messagebox.showerror('Export Error', error_msg, parent=self)
        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_export_cancelled(self):
        self.status_var.set('Bulk export cancelled.')

        if self.current_output_path and os.path.exists(self.current_output_path):
            try:
                os.remove(self.current_output_path)
            except Exception:
                pass

        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_cancel(self):
        if self.process and self.process.poll() is None:
            self.cancelled = True
            self.status_var.set('Cancelling...')
            self.cancel_btn.config(state='disabled')
        elif self.export_thread and self.export_thread.is_alive():
            self.cancelled = True
            self.status_var.set('Cancelling...')
            self.cancel_btn.config(state='disabled')
        else:
            self.destroy()
