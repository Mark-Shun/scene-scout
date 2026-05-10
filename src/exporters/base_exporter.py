import os
import re
import sys
import subprocess
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Optional

import av

import config
import gui_utils
from gui_utils import ToolTip

_FFMPEG_CACHE = None


class BaseExporter(tk.Toplevel):
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

    def __init__(self, parent):
        super().__init__(parent)

        self.parent = parent
        self.process: Optional[subprocess.Popen] = None
        self.export_thread = None
        self.cancelled = False

        gui_utils.apply_window_icon(self, getattr(parent, 'app_icon', None))

        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self.style = ttk.Style(self)
        parent_style = parent.style if hasattr(parent, 'style') else None
        if parent_style:
            self.style.theme_use(parent_style.theme_use())

        self.config = config.load_config()

    def _get_core_ffmpeg_args(self, metadata: dict) -> list:
        """Returns encoding, audio, mapping, and sync arguments."""
        args = self._get_video_encode_args()
        args.extend(self._get_audio_args())

        # Standard stream mapping
        args.extend(['-map', '0:v:0'])
        if metadata.get('has_audio'):
            args.extend(['-map', '0:a?'])

        # Global synchronization flags
        args.extend(['-avoid_negative_ts', 'make_zero', '-y'])
        return args

    def _setup_scrollable_container(self):
        """Creates a scrollable area for the dialog content."""
        self.main_canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.main_canvas.yview)
        
        self.scrollable_frame = ttk.Frame(self.main_canvas, padding="10")
        
        self.canvas_window = self.main_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        self.main_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollable_frame.bind("<Configure>", lambda e: self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all")))
        self.main_canvas.bind("<Configure>", lambda e: self.main_canvas.itemconfig(self.canvas_window, width=e.width))
        
        self.main_canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        self.main_canvas.bind_all("<MouseWheel>", lambda e: self.main_canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        
        return self.scrollable_frame

    def _build_mode_section(self, parent):
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

        ToolTip(
            frame,
            'Stream Copy cuts on keyframes only. The cut timing may not be exact.\n'
            'Re-encode mode provides exact frame accuracy but takes longer.'
        )

    def _build_video_options(self, parent):
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
        self.res_combo.bind('<<ComboboxSelected>>', lambda _e: self._update_widget_states())

        self.custom_res_frame = ttk.Frame(parent)
        self.width_var = tk.StringVar(self, value=str(self.config.get('export_custom_width', '1920')))
        self.height_var = tk.StringVar(self, value=str(self.config.get('export_custom_height', '1080')))

        ttk.Entry(self.custom_res_frame, textvariable=self.width_var, width=6).pack(side='left')
        ttk.Label(self.custom_res_frame, text='x').pack(side='left', padx=2)
        ttk.Entry(self.custom_res_frame, textvariable=self.height_var, width=6).pack(side='left')

        self.custom_res_frame.grid_forget()

    def _build_codec_option(self, parent):
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
            side='left',
            padx=(5, 0)
        )

    def _build_audio_options(self, parent):
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
        self.audio_mode_combo.bind('<<ComboboxSelected>>', lambda _e: self._update_widget_states())

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

    def _build_button_section(self, parent, export_text='Export'):
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

        self.export_btn = ttk.Button(btn_frame, text=export_text, command=self._start_export)
        self.export_btn.pack(side='left', fill='x', expand=True, padx=(0, 5))

        self.cancel_btn = ttk.Button(btn_frame, text='Cancel', command=self._on_cancel)
        self.cancel_btn.pack(side='left', fill='x', expand=True, padx=(5, 0))

    def _update_widget_states(self):
        is_encode = self.mode_var.get() == 'encode'
        audio_mode = self.audio_mode_var.get()

        if hasattr(self, 'res_combo'):
            self.res_combo.config(state='readonly' if is_encode else 'disabled')

        if hasattr(self, 'video_codec_combo'):
            self.video_codec_combo.config(state='readonly' if is_encode else 'disabled')

        if hasattr(self, 'crf_spinbox'):
            self.crf_spinbox.config(state='normal' if is_encode else 'disabled')

        is_reencode_audio = audio_mode == 'Re-encode Audio'

        if hasattr(self, 'audio_codec_combo'):
            self.audio_codec_combo.config(state='readonly' if is_reencode_audio else 'disabled')

        if hasattr(self, 'audio_bitrate_combo'):
            self.audio_bitrate_combo.config(state='readonly' if is_reencode_audio else 'disabled')

        if hasattr(self, 'custom_res_frame') and hasattr(self, 'resolution_var'):
            if self.resolution_var.get() == 'Custom' and is_encode:
                self.custom_res_frame.grid(row=0, column=2, padx=(8, 0), sticky='w')
            else:
                self.custom_res_frame.grid_forget()

        if hasattr(self, 'keyframe_info_var'):
            self.keyframe_info_var.set(self._get_keyframe_info())

    def _save_common_settings(self):
        self.config['export_mode'] = self.mode_var.get()
        self.config['export_resolution'] = self.resolution_var.get()
        self.config['export_audio_mode'] = self.audio_mode_var.get()
        self.config['export_video_codec'] = self.video_codec_var.get()
        self.config['export_audio_codec'] = self.audio_codec_var.get()
        self.config['export_crf'] = self.crf_var.get()
        self.config['export_audio_bitrate'] = self.audio_bitrate_var.get()
        self.config['export_open_folder'] = self.open_folder_var.get()
        self.config['export_custom_width'] = self.width_var.get()
        self.config['export_custom_height'] = self.height_var.get()

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

    def _get_video_encode_args(self) -> list:
        args = []
        codec_name = self.VIDEO_CODECS.get(self.video_codec_var.get(), 'libx264')

        args.extend(['-c:v', codec_name])
        args.extend(['-crf', str(self.crf_var.get())])

        res_choice = self.resolution_var.get()
        if res_choice == 'Custom':
            args.extend(['-vf', f'scale={self.width_var.get()}:{self.height_var.get()}'])
        else:
            target_height = self.RESOLUTION_PRESETS.get(res_choice)
            if target_height:
                args.extend(['-vf', f'scale=-2:{target_height}'])

        return args

    def _get_audio_args(self) -> list:
        audio_mode = self.AUDIO_MODES.get(self.audio_mode_var.get())

        if audio_mode == 'disable':
            return ['-an']

        if audio_mode == 'copy':
            return ['-c:a', 'copy']

        codec_name = self.AUDIO_CODECS.get(self.audio_codec_var.get(), 'aac')
        return ['-c:a', codec_name, '-b:a', self.audio_bitrate_var.get()]

    def _parse_time_to_ms(self, time_str: str) -> int:
        parts = time_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        sec_parts = parts[2].split('.')
        seconds = int(sec_parts[0])
        milliseconds = int(sec_parts[1]) if len(sec_parts) > 1 else 0

        return ((hours * 3600 + minutes * 60 + seconds) * 1000) + milliseconds

    def _format_ms(self, ms: int) -> str:
        hours = ms // 3600000
        mins = (ms % 3600000) // 60000
        secs = (ms % 60000) // 1000
        ms_remainder = ms % 1000

        if hours > 0:
            return f'{hours}:{mins:02d}:{secs:02d}.{ms_remainder:03d}'

        return f'{mins}:{secs:02d}.{ms_remainder:03d}'

    def _start_export(self):
        raise NotImplementedError

    def _on_cancel(self):
        raise NotImplementedError

    def _get_keyframe_info(self) -> str:
        return ''

    def _resolve_naming_template(self, template: str, metadata: dict, video_path: str, start_ms: int, end_ms: int, scene_idx: int = 0) -> str:
        from datetime import date

        tags = {
            '{source-name}': os.path.splitext(os.path.basename(video_path))[0],
            '{scene-id}': str(scene_idx + 1),
            '{time-start}': f"{start_ms / 1000.0:.1f}s",
            '{time-end}': f"{end_ms / 1000.0:.1f}s",
            '{duration}': f"{(end_ms - start_ms) / 1000.0:.1f}s",
            '{codec}': metadata.get('video_codec', 'unknown'),
            '{res}': f"{metadata.get('width', 0)}x{metadata.get('height', 0)}",
            '{date-today}': date.today().isoformat(),
        }

        result = template
        for tag, value in tags.items():
            result = result.replace(tag, value)

        sanitized = re.sub(r'[*?:"<>|]', "_", result)
        return os.path.normpath(sanitized)

    def _init_naming_vars(self):
        """Initialize shared naming variables used by both exporters."""
        self.template_var = tk.StringVar(self, value=self.config.get('naming_template', '{source-name}_scene_{time-start}'))
        self.container_var = tk.StringVar(self, value=self.config.get('export_container', 'MP4 (.mp4)'))
        self.output_path_var = tk.StringVar(self)
        self.output_dir_var = tk.StringVar(self)

    def _setup_scrollable_container(self):
        main = ttk.Frame(self, padding='10')
        main.pack(fill='both', expand=True)
        return main

    def _build_naming_section(self, parent, is_bulk=False):
        """Merged naming UI for both Single and Bulk dialogs."""
        frame = ttk.LabelFrame(parent, text='Output & Naming', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        path_row = ttk.Frame(frame)
        path_row.pack(fill='x', pady=(0, 5))
        target_var = self.output_dir_var if is_bulk else self.output_path_var
        ttk.Entry(path_row, textvariable=target_var).pack(side='left', fill='x', expand=True)
        browse_cmd = self._browse_output_dir if is_bulk else self._browse_output
        ttk.Button(path_row, text='Browse...', command=browse_cmd).pack(side='left', padx=(5, 0))

        temp_row = ttk.Frame(frame)
        temp_row.pack(fill='x', pady=5)
        ttk.Label(temp_row, text="Template:").pack(side='left')
        self.template_entry = ttk.Entry(temp_row, textvariable=self.template_var)
        self.template_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.template_var.trace_add('write', lambda *args: self._update_preview_display())

        self.tag_options = {
            "Original Name": "{source-name}", "Date": "{date-today}", "Scene ID": "{scene-id}",
            "Start": "{time-start}", "End": "{time-end}", "Codec": "{codec}", "Res": "{res}",
        }
        self.tag_selector = ttk.Combobox(temp_row, values=list(self.tag_options.keys()), state='readonly', width=10)
        self.tag_selector.set("Insert...")
        self.tag_selector.pack(side='left')
        self.tag_selector.bind("<<ComboboxSelected>>", self._on_tag_selected)

        ttk.Label(frame, text="Filename Preview:", font=('', 8, 'bold')).pack(anchor='w', pady=(5, 0))
        preview_box = ttk.Frame(frame)
        preview_box.pack(fill='x', pady=(2, 0))

        self.preview_text = tk.Text(preview_box, height=2, wrap='char', font=('', 8, 'italic'),
                                    padx=5, pady=5, bg=self.style.lookup('TFrame', 'background'), relief='flat')
        scroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=scroll.set)
        self.preview_text.pack(side='left', fill='x', expand=True)
        scroll.pack(side='right', fill='y')

    def _on_tag_selected(self, event):
        tag = self.tag_options.get(self.tag_selector.get())
        if tag:
            self.template_entry.insert(tk.INSERT, tag)
            self.tag_selector.set("Insert...")

    def _update_preview_display(self):
        """Internal helper to refresh the preview text box."""
        if not hasattr(self, 'preview_text'):
            return

        metadata, v_path, s_ms, e_ms = self._get_preview_params()
        filename = self._resolve_naming_template(self.template_var.get(), metadata, v_path, s_ms, e_ms)
        ext = self.CONTAINERS.get(self.container_var.get(), '.mp4')
        full_name = f"{filename}{ext}"

        self.preview_text.config(state='normal')
        self.preview_text.delete('1.0', tk.END)
        self.preview_text.insert(tk.END, full_name)
        self.preview_text.config(state='disabled')

        if hasattr(self, 'output_path_var') and not self.output_path_var.get().endswith(full_name):
            folder = os.path.dirname(self.output_path_var.get() or v_path)
            self.output_path_var.set(os.path.join(folder, full_name))

    def _get_preview_params(self):
        """Override in subclasses to provide scene-specific preview data."""
        return {}, 'video.mp4', 0, 10000


def get_video_info_and_keyframe(video_path: str, target_ms: int) -> Dict[str, Any]:
    info = {
        'width': 1920,
        'height': 1080,
        'framerate': 30.0,
        'has_audio': False,
        'audio_codec': None,
        'video_codec': None,
        'duration_ms': 0,
        'keyframe_ms': target_ms,
        'error': None
    }

    container = None

    try:
        container = av.open(video_path)
        video_stream = container.streams.video[0]

        info['width'] = video_stream.width
        info['height'] = video_stream.height
        info['framerate'] = float(video_stream.average_rate) if video_stream.average_rate else 30.0
        info['video_codec'] = video_stream.codec.name

        audio_streams = [s for s in container.streams if s.type == 'audio']
        info['has_audio'] = len(audio_streams) > 0

        if info['has_audio']:
            info['audio_codec'] = audio_streams[0].codec.name

        if container.duration:
            info['duration_ms'] = int((container.duration / av.time_base) * 1000)

        target_pts = int((target_ms / 1000.0) / float(video_stream.time_base))

        try:
            container.seek(target_pts, stream=video_stream, backward=True, any_frame=False)
        except Exception:
            pass

        for packet in container.demux(video_stream):
            if packet.pts is not None:
                info['keyframe_ms'] = int(packet.pts * float(video_stream.time_base) * 1000)
                break

        return info

    except Exception as e:
        info['error'] = str(e)
        return info

    finally:
        if container:
            container.close()


VIDEO_CODEC_MAP = {
    'H.264 (libx264)': 'libx264',
    'H.265 (libx265)': 'libx265',
    'AV1 (libsvtav1)': 'libsvtav1',
    'VP9 (libvpx-vp9)': 'libvpx-vp9',
    'ProRes 422 (prores_ks)': 'prores_ks'
}


def build_ffmpeg_args_headless(config_dict: dict, metadata: dict) -> list:
    """Generates encoding arguments based on a config dictionary for CLI/Headless use."""
    args = []

    codec = config_dict.get('export_video_codec', 'H.264 (libx264)')
    codec_name = VIDEO_CODEC_MAP.get(codec, 'libx264')
    args.extend(['-c:v', codec_name, '-crf', str(config_dict.get('export_crf', 23))])

    res_choice = config_dict.get('export_resolution', 'Original')
    if res_choice == 'Custom':
        args.extend(['-vf', f"scale={config_dict.get('export_custom_width', 1920)}:{config_dict.get('export_custom_height', 1080)}"])
    elif res_choice != 'Original':
        h = re.search(r'\d+', res_choice)
        if h:
            args.extend(['-vf', f'scale=-2:{h.group()}'])

    audio_mode_raw = config_dict.get('export_audio_mode', 'Copy Audio (Fast)')
    is_no_audio = audio_mode_raw in ('disable', 'No Audio (Mute)')
    is_copy = audio_mode_raw in ('copy', 'Copy Audio', 'Copy Audio (Fast)')
    if is_no_audio:
        args.append('-an')
    elif is_copy:
        args.extend(['-c:a', 'copy'])
    else:
        acodec = config_dict.get('export_audio_codec', 'AAC (aac)')
        args.extend(['-c:a', acodec, '-b:a', config_dict.get('export_audio_bitrate', '192k')])

    return args


def export_video_scene(video_path: str, start_ms: int, end_ms: int, output_path: str) -> None:
    duration_ms = end_ms - start_ms
    start_sec = start_ms / 1000.0
    duration_sec = duration_ms / 1000.0

    buffer_sec = 10.0
    if start_sec > buffer_sec:
        fast_seek = start_sec - buffer_sec
        exact_seek = buffer_sec
    else:
        fast_seek = 0.0
        exact_seek = start_sec

    app_config = config.load_config()
    metadata = get_video_info_and_keyframe(video_path, start_ms)

    cmd = [
        _get_cached_ffmpeg_path(),
        '-ss', str(fast_seek),
        '-i', video_path,
        '-ss', str(exact_seek),
    ]

    cmd.extend(build_ffmpeg_args_headless(app_config, metadata))
    cmd.extend(['-map', '0:v:0'])
    if metadata.get('has_audio'):
        cmd.extend(['-map', '0:a?'])
    cmd.extend(['-t', str(duration_sec), '-avoid_negative_ts', 'make_zero', '-y', output_path])

    creation_flags = 0
    if sys.platform == 'win32':
        creation_flags = subprocess.CREATE_NO_WINDOW

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags
    )
    _stdout, stderr = process.communicate()

    if process.returncode != 0:
        raise RuntimeError(f'FFmpeg failed with code {process.returncode}: {stderr.decode()}')


def _get_cached_ffmpeg_path() -> str:
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
