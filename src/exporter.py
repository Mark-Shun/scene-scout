import os
import re
import sys
import time
import subprocess
import av
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional, Dict, Any

import gui_utils
from gui_utils import ToolTip
import config

class SceneExportDialog(tk.Toplevel):
    """Dialog for exporting a video scene with customizable options."""

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

    def __init__(self, parent, video_path: str, start_ms: int, end_ms: int):
        super().__init__(parent)
        self.parent = parent
        
        gui_utils.apply_window_icon(self, getattr(parent, 'app_icon', None))
        
        self.video_path = video_path
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.duration_ms = end_ms - start_ms

        self.process: Optional[subprocess.Popen] = None
        self.export_thread: Optional[threading.Thread] = None
        self.cancelled = False

        self.title('Export Scene')
        self.transient(parent)
        self.grab_set()

        # Apply the parent's theme to this dialog
        self.style = ttk.Style(self)
        parent_style = parent.style if hasattr(parent, 'style') else None
        if parent_style:
            current_theme = parent_style.theme_use()
            self.style.theme_use(current_theme)

        # Load saved export settings
        self.config = config.load_config()

        self._extract_metadata()
        self._build_ui()
        gui_utils.center_window(self, 500, 680)
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

    def _extract_metadata(self):
        """Extract metadata and nearest keyframe from the source video."""
        self.metadata = get_video_info_and_keyframe(self.video_path, self.start_ms)

    def _build_ui(self):
        """Build the dialog UI components."""
        main = ttk.Frame(self, padding='10')
        main.pack(fill='both', expand=True)

        self._build_output_section(main)
        self._build_mode_section(main)
        self._build_video_options(main)
        self._build_audio_options(main)
        self._build_progress_section(main)
        self._build_button_section(main)
        self._update_widget_states()

    def _build_output_section(self, parent):
        """Build output path selection section."""
        frame = ttk.LabelFrame(parent, text='Output', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        self.output_path_var = tk.StringVar(self, value=self._generate_default_output())

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill='x')

        ttk.Entry(path_frame, textvariable=self.output_path_var).pack(side='left', fill='x', expand=True)
        ttk.Button(path_frame, text='Browse...', command=self._browse_output).pack(side='left', padx=(5, 0))

    def _build_mode_section(self, parent):
        """Build export mode selection (Stream Copy vs Re-encode)."""
        frame = ttk.LabelFrame(parent, text='Export Mode', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        # Default to saved config (Re-encode is default now)
        self.mode_var = tk.StringVar(self, value=self.config.get('export_mode', 'encode'))

        ttk.Radiobutton(frame, text='Stream Copy (Fast, Lossless)', variable=self.mode_var,
                        value='copy', command=self._update_widget_states).pack(anchor='w')
        ttk.Radiobutton(frame, text='Re-encode (Exact Frame Accuracy)', variable=self.mode_var,
                        value='encode', command=self._update_widget_states).pack(anchor='w')

        tooltip_text = ('Stream Copy cuts on keyframes only. The cut timing may not be exact.\n'
                       'Re-encode mode provides exact frame accuracy but takes longer.')
        ToolTip(frame, tooltip_text)

    def _build_video_options(self, parent):
        """Build video encoding options section."""
        self.video_frame = ttk.LabelFrame(parent, text='Video Options', padding='10')
        self.video_frame.pack(fill='x', pady=(0, 10))

        self._build_resolution_option(self.video_frame)
        self._build_codec_option(self.video_frame)
        self._build_crf_option(self.video_frame)

    def _build_resolution_option(self, parent):
        ttk.Label(parent, text='Resolution:').grid(row=0, column=0, sticky='w', pady=2)
        
        self.resolution_var = tk.StringVar(self, value='Original')
        res_combo = ttk.Combobox(parent, textvariable=self.resolution_var, 
                                values=list(self.RESOLUTION_PRESETS.keys()), state='readonly')
        # Use sticky='w' to keep the dropdown to the left
        res_combo.grid(row=0, column=1, sticky='w', padx=(10, 0))
        res_combo.bind('<<ComboboxSelected>>', lambda e: self._update_widget_states())

        # Custom Resolution Fields
        self.custom_res_frame = ttk.Frame(parent)
        self.width_var = tk.StringVar(self, value='1920')
        self.height_var = tk.StringVar(self, value='1080')
        
        ttk.Entry(self.custom_res_frame, textvariable=self.width_var, width=6).pack(side='left')
        ttk.Label(self.custom_res_frame, text='x').pack(side='left', padx=2)
        ttk.Entry(self.custom_res_frame, textvariable=self.height_var, width=6).pack(side='left')
        
        # Initialize hidden
        self.custom_res_frame.grid_forget()

    def _build_codec_option(self, parent):
        """Build video codec dropdown."""
        ttk.Label(parent, text='Video Codec:').grid(row=1, column=0, sticky='w', pady=2)

        # Use saved codec or default
        saved_codec = self.config.get('export_video_codec', 'H.264 (libx264)')
        self.video_codec_var = tk.StringVar(self, value=saved_codec)
        codec_combo = ttk.Combobox(parent, textvariable=self.video_codec_var,
                                   values=list(self.VIDEO_CODECS.keys()),
                                   state='readonly', width=20)
        codec_combo.grid(row=1, column=1, sticky='w', padx=(10, 0), pady=2)

    def _build_crf_option(self, parent):
        """Build CRF quality spinbox."""
        ttk.Label(parent, text='Quality (CRF):').grid(row=2, column=0, sticky='w', pady=2)

        saved_crf = self.config.get('export_crf', 23)
        self.crf_var = tk.IntVar(self, value=saved_crf)
        crf_frame = ttk.Frame(parent)
        crf_frame.grid(row=2, column=1, sticky='w', padx=(10, 0), pady=2)

        ttk.Spinbox(crf_frame, from_=0, to=51, textvariable=self.crf_var, width=8).pack(side='left')
        ttk.Label(crf_frame, text='(0=best, 51=worst, 23=default)').pack(side='left', padx=(5, 0))

    def _build_audio_options(self, parent):
        """Build audio options section."""
        frame = ttk.LabelFrame(parent, text='Audio Options', padding='10')
        frame.pack(fill='x', pady=(0, 10))

        # Audio mode
        saved_audio_mode = self.config.get('export_audio_mode', 'Copy Audio')
        self.audio_mode_var = tk.StringVar(self, value=saved_audio_mode)

        ttk.Label(frame, text='Audio Mode:').grid(row=0, column=0, sticky='w', pady=2)
        audio_combo = ttk.Combobox(frame, textvariable=self.audio_mode_var,
                                   values=list(self.AUDIO_MODES.keys()),
                                   state='readonly', width=20)
        audio_combo.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=2)
        audio_combo.bind('<<ComboboxSelected>>', lambda e: self._update_widget_states())

        # Audio codec
        saved_audio_codec = self.config.get('export_audio_codec', 'AAC (aac)')
        self.audio_codec_var = tk.StringVar(self, value=saved_audio_codec)
        self.audio_bitrate_var = tk.StringVar(self, value=self.config.get('export_audio_bitrate', '192k'))

        ttk.Label(frame, text='Audio Codec:').grid(row=1, column=0, sticky='w', pady=2)
        self.audio_codec_combo = ttk.Combobox(frame, textvariable=self.audio_codec_var,
                                              values=list(self.AUDIO_CODECS.keys()),
                                              state='readonly', width=20)
        self.audio_codec_combo.grid(row=1, column=1, sticky='w', padx=(10, 0), pady=2)

        ttk.Label(frame, text='Audio Bitrate:').grid(row=2, column=0, sticky='w', pady=2)
        self.audio_bitrate_combo = ttk.Combobox(frame, textvariable=self.audio_bitrate_var,
                                                 values=['128k', '192k', '256k', '320k'],
                                                 state='readonly', width=20)
        self.audio_bitrate_combo.grid(row=2, column=1, sticky='w', padx=(10, 0), pady=2)

    def _build_progress_section(self, parent):
        """Build progress bar and status section."""
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=(0, 10))

        self.progress_var = tk.DoubleVar(self, value=0.0)
        self.progress_bar = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', pady=(0, 5))

        self.status_var = tk.StringVar(self, value='Ready')
        ttk.Label(frame, textvariable=self.status_var).pack(anchor='w')

        # Dynamic keyframe info label
        self.keyframe_info_var = tk.StringVar(self, value=self._get_keyframe_info())
        self.keyframe_label = ttk.Label(frame, textvariable=self.keyframe_info_var, font=('', 8))
        self.keyframe_label.pack(anchor='w')

    def _get_keyframe_info(self) -> str:
        """Get the keyframe info string based on current mode."""
        if self.mode_var.get() == 'copy':
            return f"Note: Stream Copy snaps to keyframe at {self._format_ms(self.metadata['keyframe_ms'])}, timing may not be exact"
        else:
            return f"Exact frame accuracy: {self._format_ms(self.start_ms)}"

    def _build_button_section(self, parent):
        """Build Export/Cancel buttons and options."""
        frame = ttk.Frame(parent)
        frame.pack(fill='x')

        # Open folder checkbox
        self.open_folder_var = tk.BooleanVar(self, value=self.config.get('export_open_folder', True))
        self.open_folder_check = ttk.Checkbutton(frame, text='Open folder after export',
                                              variable=self.open_folder_var)
        self.open_folder_check.pack(side='top', anchor='w', pady=(0, 5))

        # Buttons frame
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill='x')

        self.export_btn = ttk.Button(btn_frame, text='Export', command=self._start_export)
        self.export_btn.pack(side='left', fill='x', expand=True, padx=(0, 5))

        self.cancel_btn = ttk.Button(btn_frame, text='Cancel', command=self._on_cancel)
        self.cancel_btn.pack(side='left', fill='x', expand=True, padx=(5, 0))

    def _generate_default_output(self) -> str:
        """Generate default output filename."""
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        start_sec = self.start_ms / 1000.0
        end_sec = self.end_ms / 1000.0
        return os.path.join(os.path.dirname(self.video_path),
                          f'{base}_scene_{start_sec:.1f}s-{end_sec:.1f}s.mp4')

    def _browse_output(self):
        """Open file dialog to choose output path."""
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

    def _update_widget_states(self):
        """Enable/disable widgets based on current mode and audio settings."""
        is_encode = self.mode_var.get() == 'encode'
        audio_mode = self.audio_mode_var.get()

        for child in self.video_frame.winfo_children():
            if isinstance(child, (ttk.Label, ttk.Frame)):
                continue
            state = 'normal' if is_encode else 'disabled'
            try:
                child.config(state=state)
            except Exception:
                pass

        is_reencode_audio = audio_mode == 'Re-encode Audio'
        self.audio_codec_combo.config(state='normal' if is_reencode_audio else 'disabled')
        self.audio_bitrate_combo.config(state='normal' if is_reencode_audio else 'disabled')

        # Update keyframe info label
        self.keyframe_info_var.set(self._get_keyframe_info())
        
        if self.resolution_var.get() == 'Custom' and self.mode_var.get() == 'encode':
            self.custom_res_frame.grid(row=0, column=2, padx=(0, 0), sticky='w')
        else:
            self.custom_res_frame.grid_forget()

    def _save_settings(self):
        """Save current export settings to config."""
        self.config['export_mode'] = self.mode_var.get()
        self.config['export_resolution'] = self.resolution_var.get()
        self.config['export_audio_mode'] = self.audio_mode_var.get()
        self.config['export_video_codec'] = self.video_codec_var.get()
        self.config['export_audio_codec'] = self.audio_codec_var.get()
        self.config['export_crf'] = self.crf_var.get()
        self.config['export_audio_bitrate'] = self.audio_bitrate_var.get()
        self.config['export_open_folder'] = self.open_folder_var.get()
        config.save_config(self.config)

    def _start_export(self):
        """Start the export process in a background thread."""
        output_path = self.output_path_var.get()
        if not output_path:
            messagebox.showerror('Error', 'Please specify an output path.')
            return

        if os.path.exists(output_path):
            if not messagebox.askyesno('Overwrite?', f'{os.path.basename(output_path)} already exists. Overwrite?'):
                return

        # Save settings for next time
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
        """Background thread task for running FFmpeg."""
        try:
            cmd = self._build_ffmpeg_command()
            self._run_ffmpeg(cmd)
        except Exception as e:
            self.after(0, lambda: self._on_export_error(str(e)))

    def _build_ffmpeg_command(self) -> list:
        """Build the FFmpeg command based on current settings."""
        cmd = [self._get_ffmpeg_path()]

        if self.mode_var.get() == 'copy':
            # Stream copy: Use PyAV-calculated keyframe timestamp
            start_sec = self.metadata['keyframe_ms'] / 1000.0
            cmd.extend(['-ss', str(start_sec)])
            cmd.extend(['-i', self.video_path])
            cmd.extend(['-c', 'copy'])
        else:
            # Re-encode: Two-Step "Fast & Accurate" Seek
            start_sec = self.start_ms / 1000.0
            duration_sec = self.duration_ms / 1000.0

            # Fast-seek to 10s before target, then accurately decode and drop to ensure perfect A/V sync
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

        # Strip broken metadata/data streams that can break container timestamps
        # Keep the primary video stream and all audio streams
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

    def _get_ffmpeg_path(self) -> str:
        """Get path to FFmpeg binary."""
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass

        import shutil
        ffmpeg_path = shutil.which('ffmpeg')
        if ffmpeg_path:
            return ffmpeg_path

        return 'ffmpeg'  # Fallback, may fail but provides clear error
    
    def _get_video_encode_args(self) -> list:
        args = []
        codec_name = self.VIDEO_CODECS.get(self.video_codec_var.get(), 'libx264')
        args.extend(['-c:v', codec_name])
        args.extend(['-crf', str(self.crf_var.get())])

        res_choice = self.resolution_var.get()
        if res_choice == 'Custom':
            # Ensure dimensions are even numbers (requirement for many codecs)
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
        elif audio_mode == 'copy':
            return ['-c:a', 'copy']
        else:
            args = []
            codec_name = self.AUDIO_CODECS.get(self.audio_codec_var.get(), 'aac')
            args.extend(['-c:a', codec_name])
            args.extend(['-b:a', self.audio_bitrate_var.get()])
            return args

    def _run_ffmpeg(self, cmd: list):
        """Execute FFmpeg and parse progress."""
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

        time_regex = re.compile(r'time=(\d+:\d+:\d+\.\d+)')

        for line in self.process.stderr:
            if self.cancelled:
                self.process.terminate()
                self.process.wait()
                self.after(0, self._on_export_cancelled)
                return

            match = time_regex.search(line)
            if match:
                time_str = match.group(1)
                current_ms = self._parse_time_to_ms(time_str)
                progress = min(100.0, (current_ms / self.duration_ms) * 100.0)
                self.after(0, lambda p=progress: self._update_progress(
                    p, f'Exporting... {self._format_ms(current_ms)} / {self._format_ms(self.duration_ms)}'))

        self.process.wait()

        if self.process.returncode == 0 and not self.cancelled:
            self.after(0, self._on_export_complete)
        elif not self.cancelled:
            stderr_output = ''
            if self.process.stderr:
                stderr_output = ''.join(self.process.stderr.readlines())
            self.after(0, lambda: self._on_export_error(
                f'FFmpeg exited with code {self.process.returncode}\n{stderr_output}'))

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

    def _update_progress(self, progress: float, status: str):
        """Update progress bar and status label (called from main thread)."""
        self.progress_var.set(progress)
        self.status_var.set(status)

    def _check_export_progress(self):
        """Periodically check if export thread is still running."""
        if self.export_thread and self.export_thread.is_alive():
            self.after(100, self._check_export_progress)
        else:
            self.export_btn.config(state='normal')

    def _on_export_complete(self):
        """Handle successful export completion."""
        self.progress_var.set(100)
        self.status_var.set('Export complete!')
        output_path = self.output_path_var.get()
        messagebox.showinfo('Success', f'Scene exported successfully to:\n{output_path}')

        # Open folder and highlight file if checkbox is checked
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
                print(f"Failed to open output directory: {e}")

        self.destroy()

    def _on_export_error(self, error_msg: str):
        """Handle export error."""
        self.status_var.set('Export failed!')
        messagebox.showerror('Export Error', error_msg)
        self.export_btn.config(state='normal')

    def _on_export_cancelled(self):
        """Handle export cancellation."""
        self.status_var.set('Export cancelled.')
        output_path = self.output_path_var.get()
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        self.export_btn.config(state='normal')

    def _on_cancel(self):
        """Handle cancel button or window close."""
        if self.process and self.process.poll() is None:
            self.cancelled = True
            self.status_var.set('Cancelling...')
            self.cancel_btn.config(state='disabled')
        else:
            self.destroy()

def get_video_info_and_keyframe(video_path: str, target_ms: int) -> Dict[str, Any]:
    """Extract video metadata AND nearest keyframe in a single pass with O(1) seeking."""
    info = {
        'width': 1920, 'height': 1080, 'framerate': 30.0,
        'has_audio': False, 'audio_codec': None, 'video_codec': None,
        'duration_ms': 0, 'keyframe_ms': target_ms, 'error': None
    }
    container = None
    try:
        container = av.open(video_path)
        video_stream = container.streams.video[0]
        
        # Metadata extraction
        info['width'], info['height'] = video_stream.width, video_stream.height
        info['framerate'] = float(video_stream.average_rate) if video_stream.average_rate else 30.0
        info['video_codec'] = video_stream.codec.name
        audio_streams = [s for s in container.streams if s.type == 'audio']
        info['has_audio'] = len(audio_streams) > 0
        if info['has_audio']: info['audio_codec'] = audio_streams[0].codec.name
        if container.duration: info['duration_ms'] = int((container.duration / av.time_base) * 1000)

        # Jump directly to target to avoid scanning the whole file
        target_pts = int((target_ms / 1000.0) / float(video_stream.time_base))
        try:
            container.seek(target_pts, stream=video_stream, backward=True, any_frame=False)
        except Exception: pass

        for packet in container.demux(video_stream):
            if packet.pts is not None:
                info['keyframe_ms'] = int(packet.pts * float(video_stream.time_base) * 1000)
                break
        return info
    except Exception as e:
        info['error'] = str(e)
        return info
    finally:
        if container: container.close()