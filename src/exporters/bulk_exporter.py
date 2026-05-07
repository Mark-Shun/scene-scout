import os
import re
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional, Dict, Any

import gui_utils
from gui_utils import ToolTip
import config

from utils import _get_ffmpeg_path
from .single_exporter import get_video_info_and_keyframe

_FFMPEG_CACHE = None

class BulkExportDialog(tk.Toplevel):
    """Dialog for exporting multiple video scenes with shared export options."""

    RESOLUTION_PRESETS = {
        'Original': None,
        '4K (2160p)': 2160,
        '1440p': 1440,
        '1080p': 1080,
        '720p': 720,
        '480p': 480,
        'Custom': 'custom'
    }

    VIDEO_CODECS = {
        'H.264 (libx264)': 'libx264',
        'H.265 (libx265)': 'libx265',
        'AV1 (libsvtav1)': 'libsvtav1',
        'VP9 (libvpx-vp9)': 'libvpx-vp9',
        'ProRes 422 (prores_ks)': 'prores_ks'
    }

    CONTAINERS = {
        'MP4 (.mp4)': '.mp4',
        'Matroska (.mkv)': '.mkv',
        'QuickTime (.mov)': '.mov',
        'WebM (.webm)': '.webm'
    }

    AUDIO_CODECS = {
        'AAC (aac)': 'aac',
        'MP3 (libmp3lame)': 'libmp3lame',
        'Opus (libopus)': 'libopus'
    }

    AUDIO_MODES = {
        'Copy Audio (Fast)': 'copy',
        'Re-encode Audio': 'encode',
        'No Audio (Mute)': 'disable'
    }

    def __init__(self, parent, scenes: list):
        """
        changed parameters from SceneExportDialog: 
         scenes -> list of (video_path, start_ms, end_ms) tuples
        """
        super().__init__(parent)

        self.parent = parent
        self.scenes = scenes

        self.process: Optional[subprocess.Popen] = None
        self.export_thread: Optional[threading.Thread] = None
        self.cancelled = False

        self.current_output_path: Optional[str] = None
        self.completed_outputs: list[str] = []
        self.current_scene_idx = 0
        self.current_scene_total = len(scenes)

        gui_utils.apply_window_icon(self, getattr(parent, 'app_icon', None))

        self.title(f'Bulk Export — {len(scenes)} Scene(s)')
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self.style = ttk.Style(self)
        parent_style = parent.style if hasattr(parent, 'style') else None
        if parent_style:
            self.style.theme_use(parent_style.theme_use())

        self.config = config.load_config()

        self.metadata_by_scene: list[Dict[str, Any]] = []
        self._extract_metadata()

        self._build_ui()
        gui_utils.center_window(self, 540, 960)
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

    def _extract_metadata(self):
        """Extract basic metadata/keyframe info for each selected scene."""
        self.metadata_by_scene = []

        for video_path, start_ms, _end_ms in self.scenes:
            self.metadata_by_scene.append(
                get_video_info_and_keyframe(video_path, start_ms)
            )

    # UI builders
    def _build_ui(self):
        """Build the dialog UI components."""
        main = ttk.Frame(self, padding='10')
        main.pack(fill='both', expand=True)

        self._build_output_section(main)
        self._build_mode_section(main)
        self._build_container_section(main)
        self._build_video_options(main)
        self._build_audio_options(main)
        self._build_progress_section(main)
        self._build_button_section(main)
        self._update_widget_states()

    def _build_output_section(self, parent):
        """Build output folder selection section."""
        frame = ttk.LabelFrame(parent, text='Output', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        default_dir = self._generate_default_output_dir()
        self.output_dir_var = tk.StringVar(self, value=default_dir)

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill='x')

        ttk.Entry(path_frame, textvariable=self.output_dir_var).pack(
            side='left', fill='x', expand=True
        )
        ttk.Button(path_frame, text='Browse...', command=self._browse_output_dir).pack(
            side='left', padx=(5, 0)
        )

        self.filename_note_var = tk.StringVar(
            self,
            value='Files will be named like: video_scene_12.3s-18.6s.mp4'
        )
        ttk.Label(frame, textvariable=self.filename_note_var, font=('', 8)).pack(
            anchor='w', pady=(5, 0)
        )

    def _build_mode_section(self, parent):
        """Build export mode selection."""
        frame = ttk.LabelFrame(parent, text='Export Mode', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        self.mode_var = tk.StringVar(self, value=self.config.get('export_mode', 'encode'))

        ttk.Radiobutton(
            frame,
            text='Stream Copy (Fast, Lossless)',
            variable=self.mode_var,
            value='copy',
            command=self._update_widget_states
        ).pack(anchor='w')

        ttk.Radiobutton(
            frame,
            text='Re-encode (Exact Frame Accuracy)',
            variable=self.mode_var,
            value='encode',
            command=self._update_widget_states
        ).pack(anchor='w')

        tooltip_text = (
            'Stream Copy cuts on keyframes only. The cut timing may not be exact.\n'
            'Re-encode mode provides exact frame accuracy but takes longer.'
        )
        ToolTip(frame, tooltip_text)

    def _build_container_section(self, parent):
        """Build output container/extension section."""
        frame = ttk.LabelFrame(parent, text='Container', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        saved_container = self.config.get('export_container', 'MP4 (.mp4)')
        self.container_var = tk.StringVar(self, value=saved_container)

        ttk.Label(frame, text='Format:').grid(row=0, column=0, sticky='w', pady=2)
        container_combo = ttk.Combobox(
            frame,
            textvariable=self.container_var,
            values=list(self.CONTAINERS.keys()),
            state='readonly',
            width=20
        )
        container_combo.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=2)
        container_combo.bind('<<ComboboxSelected>>', lambda e: self._update_filename_note())

    def _build_video_options(self, parent):
        """Build video encoding options section."""
        self.video_frame = ttk.LabelFrame(parent, text='Video Options', padding='10')
        self.video_frame.pack(fill='x', pady=(0, 10))

        self._build_resolution_option(self.video_frame)
        self._build_codec_option(self.video_frame)
        self._build_crf_option(self.video_frame)

    def _build_resolution_option(self, parent):
        ttk.Label(parent, text='Resolution:').grid(row=0, column=0, sticky='w', pady=2)

        saved_resolution = self.config.get('export_resolution', 'Original')
        self.resolution_var = tk.StringVar(self, value=saved_resolution)

        self.res_combo = ttk.Combobox(
            parent,
            textvariable=self.resolution_var,
            values=list(self.RESOLUTION_PRESETS.keys()),
            state='readonly',
            width=20
        )
        self.res_combo.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=2)
        self.res_combo.bind('<<ComboboxSelected>>', lambda e: self._update_widget_states())

        self.custom_res_frame = ttk.Frame(parent)

        self.width_var = tk.StringVar(self, value=str(self.config.get('export_custom_width', '1920')))
        self.height_var = tk.StringVar(self, value=str(self.config.get('export_custom_height', '1080')))

        ttk.Entry(self.custom_res_frame, textvariable=self.width_var, width=6).pack(side='left')
        ttk.Label(self.custom_res_frame, text='x').pack(side='left', padx=2)
        ttk.Entry(self.custom_res_frame, textvariable=self.height_var, width=6).pack(side='left')

        self.custom_res_frame.grid_forget()

    def _build_codec_option(self, parent):
        """Build video codec dropdown."""
        ttk.Label(parent, text='Video Codec:').grid(row=1, column=0, sticky='w', pady=2)

        saved_codec = self.config.get('export_video_codec', 'H.264 (libx264)')
        self.video_codec_var = tk.StringVar(self, value=saved_codec)

        self.video_codec_combo = ttk.Combobox(
            parent,
            textvariable=self.video_codec_var,
            values=list(self.VIDEO_CODECS.keys()),
            state='readonly',
            width=20
        )
        self.video_codec_combo.grid(row=1, column=1, sticky='w', padx=(10, 0), pady=2)

    def _build_crf_option(self, parent):
        """Build CRF quality spinbox."""
        ttk.Label(parent, text='Quality (CRF):').grid(row=2, column=0, sticky='w', pady=2)

        saved_crf = self.config.get('export_crf', 23)
        self.crf_var = tk.IntVar(self, value=saved_crf)

        crf_frame = ttk.Frame(parent)
        crf_frame.grid(row=2, column=1, sticky='w', padx=(10, 0), pady=2)

        self.crf_spinbox = ttk.Spinbox(
            crf_frame,
            from_=0,
            to=51,
            textvariable=self.crf_var,
            width=8
        )
        self.crf_spinbox.pack(side='left')
        ttk.Label(crf_frame, text='(0=best, 51=worst, 23=default)').pack(
            side='left', padx=(5, 0)
        )

    def _build_audio_options(self, parent):
        """Build audio options section."""
        frame = ttk.LabelFrame(parent, text='Audio Options', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        saved_audio_mode = self.config.get('export_audio_mode', 'Copy Audio (Fast)')
        self.audio_mode_var = tk.StringVar(self, value=saved_audio_mode)

        ttk.Label(frame, text='Audio Mode:').grid(row=0, column=0, sticky='w', pady=2)
        self.audio_mode_combo = ttk.Combobox(
            frame,
            textvariable=self.audio_mode_var,
            values=list(self.AUDIO_MODES.keys()),
            state='readonly',
            width=20
        )
        self.audio_mode_combo.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=2)
        self.audio_mode_combo.bind('<<ComboboxSelected>>', lambda e: self._update_widget_states())

        saved_audio_codec = self.config.get('export_audio_codec', 'AAC (aac)')
        self.audio_codec_var = tk.StringVar(self, value=saved_audio_codec)
        self.audio_bitrate_var = tk.StringVar(
            self,
            value=self.config.get('export_audio_bitrate', '192k')
        )

        ttk.Label(frame, text='Audio Codec:').grid(row=1, column=0, sticky='w', pady=2)
        self.audio_codec_combo = ttk.Combobox(
            frame,
            textvariable=self.audio_codec_var,
            values=list(self.AUDIO_CODECS.keys()),
            state='readonly',
            width=20
        )
        self.audio_codec_combo.grid(row=1, column=1, sticky='w', padx=(10, 0), pady=2)

        ttk.Label(frame, text='Audio Bitrate:').grid(row=2, column=0, sticky='w', pady=2)
        self.audio_bitrate_combo = ttk.Combobox(
            frame,
            textvariable=self.audio_bitrate_var,
            values=['128k', '192k', '256k', '320k'],
            state='readonly',
            width=20
        )
        self.audio_bitrate_combo.grid(row=2, column=1, sticky='w', padx=(10, 0), pady=2)

    def _build_progress_section(self, parent):
        """Build current-scene and overall progress bars."""
        frame = ttk.LabelFrame(parent, text='Progress', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        self.scene_label_var = tk.StringVar(
            self,
            value=f'Ready to export {len(self.scenes)} scene(s)'
        )
        ttk.Label(frame, textvariable=self.scene_label_var, font=('', 10, 'bold')).pack(
            anchor='w', pady=(0, 5)
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

    def _build_button_section(self, parent):
        """Build export/cancel buttons and options."""
        frame = ttk.Frame(parent)
        frame.pack(fill='x')

        self.open_folder_var = tk.BooleanVar(
            self,
            value=self.config.get('export_open_folder', True)
        )
        self.open_folder_check = ttk.Checkbutton(
            frame,
            text='Open folder after export',
            variable=self.open_folder_var
        )
        self.open_folder_check.pack(side='top', anchor='w', pady=(0, 5))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill='x')

        self.export_btn = ttk.Button(btn_frame, text='Export All', command=self._start_export)
        self.export_btn.pack(side='left', fill='x', expand=True, padx=(0, 5))

        self.cancel_btn = ttk.Button(btn_frame, text='Cancel', command=self._on_cancel)
        self.cancel_btn.pack(side='left', fill='x', expand=True, padx=(5, 0))

    # UI helpers
    def _generate_default_output_dir(self) -> str:
        """Default output folder based on the first selected scene."""
        if not self.scenes:
            return os.getcwd()
        first_video_path = self.scenes[0][0]
        return os.path.dirname(first_video_path)

    def _generate_default_output_path(self, video_path: str, start_ms: int, end_ms: int) -> str:
        """Generate an output filename for one scene."""
        base = os.path.splitext(os.path.basename(video_path))[0]
        start_sec = start_ms / 1000.0
        end_sec = end_ms / 1000.0
        ext = self.CONTAINERS.get(self.container_var.get(), '.mp4')

        filename = f'{base}_scene_{start_sec:.1f}s-{end_sec:.1f}s{ext}'
        return os.path.join(self.output_dir_var.get(), filename)

    def _make_unique_output_path(self, output_path: str) -> str:
        """Avoid overwriting duplicate names inside the same batch."""
        if output_path not in self.completed_outputs and not os.path.exists(output_path):
            return output_path

        folder = os.path.dirname(output_path)
        stem, ext = os.path.splitext(os.path.basename(output_path))

        counter = 2
        while True:
            candidate = os.path.join(folder, f'{stem}_{counter}{ext}')
            if candidate not in self.completed_outputs and not os.path.exists(candidate):
                return candidate
            counter += 1

    def _browse_output_dir(self):
        """Choose a folder for all exported scenes."""
        path = filedialog.askdirectory(
            title='Choose Bulk Export Folder',
            initialdir=self.output_dir_var.get() or os.getcwd()
        )
        if path:
            self.output_dir_var.set(path)

    def _update_filename_note(self):
        """Update the output naming hint after changing container."""
        ext = self.CONTAINERS.get(self.container_var.get(), '.mp4')
        self.filename_note_var.set(
            f'Files will be named like: video_scene_12.3s-18.6s{ext}'
        )

    def _get_keyframe_info(self) -> str:
        """Get keyframe info based on current mode."""
        if not self.scenes:
            return ''

        _video_path, start_ms, _end_ms = self.scenes[self.current_scene_idx]
        metadata = self.metadata_by_scene[self.current_scene_idx] if self.metadata_by_scene else {}

        if self.mode_var.get() == 'copy':
            keyframe_ms = metadata.get('keyframe_ms', start_ms)
            return (
                f'Note: Stream Copy snaps each scene to its nearest keyframe. '
                f'Current scene keyframe: {self._format_ms(keyframe_ms)}'
            )

        return f'Exact frame accuracy for current scene: {self._format_ms(start_ms)}'

    def _update_widget_states(self):
        """Enable/disable widgets based on current mode and audio settings."""
        is_encode = self.mode_var.get() == 'encode'
        audio_mode = self.audio_mode_var.get()

        # Same idea as single_exporter.py, but keep labels/frames alive.
        for child in self.video_frame.winfo_children():
            if isinstance(child, (ttk.Label, ttk.Frame)):
                continue

            try:
                child.config(state='normal' if is_encode else 'disabled')
            except Exception:
                pass

        # Spinbox is inside a frame, so handle it explicitly.
        self.crf_spinbox.config(state='normal' if is_encode else 'disabled')

        is_reencode_audio = audio_mode == 'Re-encode Audio'
        self.audio_codec_combo.config(state='normal' if is_reencode_audio else 'disabled')
        self.audio_bitrate_combo.config(state='normal' if is_reencode_audio else 'disabled')

        self.keyframe_info_var.set(self._get_keyframe_info())

        if self.resolution_var.get() == 'Custom' and is_encode:
            self.custom_res_frame.grid(row=0, column=2, padx=(8, 0), sticky='w')
        else:
            self.custom_res_frame.grid_forget()

    def _save_settings(self):
        """Save current export settings to config."""
        self.config['export_mode'] = self.mode_var.get()
        self.config['export_resolution'] = self.resolution_var.get()
        self.config['export_container'] = self.container_var.get()
        self.config['export_audio_mode'] = self.audio_mode_var.get()
        self.config['export_video_codec'] = self.video_codec_var.get()
        self.config['export_audio_codec'] = self.audio_codec_var.get()
        self.config['export_crf'] = self.crf_var.get()
        self.config['export_audio_bitrate'] = self.audio_bitrate_var.get()
        self.config['export_open_folder'] = self.open_folder_var.get()
        self.config['export_custom_width'] = self.width_var.get()
        self.config['export_custom_height'] = self.height_var.get()
        config.save_config(self.config)

    # Export flow
    def _start_export(self):
        """Validate output settings and start the export thread."""
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

        # planned_outputs creates an empty list that will store all final output file paths
        planned_outputs = []
        for video_path, start_ms, end_ms in self.scenes:
            planned_outputs.append(
                self._make_unique_output_path(
                    self._generate_default_output_path(video_path, start_ms, end_ms)
                )
            )

        # go through all of planned_outputs and see if they already exist, overwrite if they do
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
        """Background task for exporting every scene in order."""
        total = len(self.scenes)

        try:
            # iterates through all selected scenes and exports them one-by-one
            for idx, (video_path, start_ms, end_ms) in enumerate(self.scenes):
                if self.cancelled:
                    self.after(0, self._on_export_cancelled)
                    return

                self.current_scene_idx = idx
                output_path = self.planned_outputs[idx]
                self.current_output_path = output_path

                # update the bulk export UI safely from the background thread
                self.after(0, lambda i=idx, total=total, vp=video_path: self.scene_label_var.set(
                    f'Exporting {i + 1}/{total}: {os.path.basename(vp)}'
                ))
                self.after(0, lambda: self.scene_progress_var.set(0))
                self.after(0, lambda p=output_path: self.status_var.set(f'{p}'))
                self.after(0, lambda: self.keyframe_info_var.set(self._get_keyframe_info()))

                # build the command
                cmd = self._build_ffmpeg_command(
                    scene_idx=idx,
                    video_path=video_path,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    output_path=output_path
                )

                # run the command
                self._run_ffmpeg(
                    cmd=cmd,
                    scene_idx=idx,
                    total_scenes=total,
                    duration_ms=end_ms - start_ms
                )

                if self.cancelled:
                    self.after(0, self._on_export_cancelled)
                    return

                self.completed_outputs.append(output_path)

            self.after(0, self._on_export_complete)

        except Exception as e:
            self.after(0, lambda err=str(e): self._on_export_error(err))

    def _get_ffmpeg_path(self) -> str:
        global _FFMPEG_CACHE
        if _FFMPEG_CACHE:
            return _FFMPEG_CACHE

        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            import shutil
            path = shutil.which('ffmpeg') or 'ffmpeg'

        _FFMPEG_CACHE = path
        return path
    
    def _build_ffmpeg_command(
        self,
        scene_idx: int,
        video_path: str,
        start_ms: int,
        end_ms: int,
        output_path: str
    ) -> list:
        """Build an FFmpeg command for one scene using the shared bulk settings."""

        # IMPORTANT DISTINCTION: 
        # SceneExportDialog used only single scenes for export, which is why it has no
        # parameters compared to this one. Since this one accepts list of scenes, they all
        # have different scene_idx, different start_ms, etc. So we pass those in as parameters
        # whereas the same function SceneExportDialog (in single_exporter.py) doesn't.
        duration_ms = end_ms - start_ms
        duration_sec = duration_ms / 1000.0

        metadata = self.metadata_by_scene[scene_idx] if scene_idx < len(self.metadata_by_scene) else {}

        cmd = [self._get_ffmpeg_path()]

        if self.mode_var.get() == 'copy':
            keyframe_ms = metadata.get('keyframe_ms', start_ms)
            start_sec = keyframe_ms / 1000.0

            cmd.extend(['-ss', str(start_sec)])
            cmd.extend(['-i', video_path])
            cmd.extend(['-c', 'copy'])
        else:
            start_sec = start_ms / 1000.0

            buffer_sec = 10.0
            if start_sec > buffer_sec:
                fast_seek = start_sec - buffer_sec
                exact_seek = buffer_sec
            else:
                fast_seek = 0.0
                exact_seek = start_sec

            cmd.extend(['-ss', str(fast_seek)])
            cmd.extend(['-i', video_path])
            cmd.extend(['-ss', str(exact_seek)])
            cmd.extend(self._get_video_encode_args())

        cmd.extend(self._get_audio_args())

        cmd.extend(['-map', '0:v:0'])
        if metadata.get('has_audio'):
            cmd.extend(['-map', '0:a?'])

        cmd.extend([
            '-t', str(duration_sec),
            '-avoid_negative_ts', 'make_zero',
            '-y',
            output_path
        ])

        return cmd

    def _get_video_encode_args(self) -> list:
        """Get video encode arguments for re-encode mode."""
        args = []

        codec_name = self.VIDEO_CODECS.get(self.video_codec_var.get(), 'libx264')
        args.extend(['-c:v', codec_name])
        args.extend(['-crf', str(self.crf_var.get())])

        res_choice = self.resolution_var.get()
        if res_choice == 'Custom':
            w, h = self.width_var.get(), self.height_var.get()
            args.extend(['-vf', f'scale={w}:{h}'])
        else:
            target_height = self.RESOLUTION_PRESETS.get(res_choice)
            if target_height:
                args.extend(['-vf', f'scale=-2:{target_height}'])

        return args

    def _get_audio_args(self) -> list:
        """Get audio-related arguments."""
        audio_mode = self.AUDIO_MODES.get(self.audio_mode_var.get())

        if audio_mode == 'disable':
            return ['-an']
        if audio_mode == 'copy':
            return ['-c:a', 'copy']

        codec_name = self.AUDIO_CODECS.get(self.audio_codec_var.get(), 'aac')
        return ['-c:a', codec_name, '-b:a', self.audio_bitrate_var.get()]

    def _run_ffmpeg(self, cmd: list, scene_idx: int, total_scenes: int, duration_ms: int):
        """Run FFmpeg for one scene and update both progress bars."""
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
                lambda op=completed_overall: self._update_progress(100.0, op, 'Scene complete.')
            )

    # Progress/time helpers
    def _parse_time_to_ms(self, time_str: str) -> int:
        """Convert HH:MM:SS.ms string to milliseconds."""
        parts = time_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        sec_parts = parts[2].split('.')
        seconds = int(sec_parts[0])
        milliseconds = int(sec_parts[1]) if len(sec_parts) > 1 else 0

        return ((hours * 3600 + minutes * 60 + seconds) * 1000) + milliseconds

    def _format_ms(self, ms: int) -> str:
        """Format milliseconds to human-readable time string."""
        hours = ms // 3600000
        mins = (ms % 3600000) // 60000
        secs = (ms % 60000) // 1000
        ms_remainder = ms % 1000

        if hours > 0:
            return f'{hours}:{mins:02d}:{secs:02d}.{ms_remainder:03d}'
        return f'{mins}:{secs:02d}.{ms_remainder:03d}'

    def _update_progress(self, scene_progress: float, overall_progress: float, status: str):
        """Update progress UI from the main thread."""
        self.scene_progress_var.set(scene_progress)
        self.overall_progress_var.set(overall_progress)
        self.status_var.set(status)

    def _check_export_progress(self):
        """Periodically check if the export thread is still running."""
        if self.export_thread and self.export_thread.is_alive():
            self.after(100, self._check_export_progress)
        else:
            self.export_btn.config(state='normal')

    # Completion / error / cancellation
    def _on_export_complete(self):
        """Handle successful bulk export completion."""
        self.scene_progress_var.set(100)
        self.overall_progress_var.set(100)
        self.status_var.set('Bulk export complete!')
        self.scene_label_var.set(f'Done! Exported {len(self.scenes)} scene(s).')

        output_dir = os.path.abspath(self.output_dir_var.get())
        messagebox.showinfo(
            'Bulk Export Complete',
            f'Successfully exported {len(self.scenes)} scene(s) to:\n{output_dir}',
            parent=self
        )

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
        """Handle export error."""
        self.status_var.set('Bulk export failed!')
        messagebox.showerror('Export Error', error_msg, parent=self)
        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_export_cancelled(self):
        """Handle export cancellation."""
        self.status_var.set('Bulk export cancelled.')

        # Remove the partial file for the scene currently being written.
        if self.current_output_path and os.path.exists(self.current_output_path):
            try:
                os.remove(self.current_output_path)
            except Exception:
                pass

        self.export_btn.config(state='normal')
        self.cancel_btn.config(state='normal')

    def _on_cancel(self):
        """Handle cancel button or window close."""
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
