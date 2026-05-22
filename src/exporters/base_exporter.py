import os
import re
import sys
import subprocess
from typing import Any, Dict, Optional
from datetime import date

import av

import config
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QComboBox,
    QProgressBar, QPushButton, QLabel, QSpinBox, QCheckBox,
    QRadioButton, QFrame, QLineEdit, QGridLayout,
    QWidget, QFileDialog
)

_FFMPEG_CACHE = None


class BaseExporter(QDialog):
    RESOLUTION_PRESETS = {
        'Original': None,
        '4K (2160p)': 2160,
        '1440p': 1440,
        '1080p': 1080,
        '720p': 720,
        '480p': 480,
        'Custom': 'custom',
    }

    VIDEO_CODECS = {
        'H.264 (libx264)': 'libx264',
        'H.265 (libx265)': 'libx265',
        'AV1 (libsvtav1)': 'libsvtav1',
        'VP9 (libvpx-vp9)': 'libvpx-vp9',
        'ProRes 422 (prores_ks)': 'prores_ks',
    }

    CONTAINERS = {
        'MP4 (.mp4)': '.mp4',
        'Matroska (.mkv)': '.mkv',
        'QuickTime (.mov)': '.mov',
        'WebM (.webm)': '.webm',
    }

    AUDIO_CODECS = {
        'AAC (aac)': 'aac',
        'MP3 (libmp3lame)': 'libmp3lame',
        'Opus (libopus)': 'libopus',
    }

    AUDIO_MODES = {
        'Copy Audio (Fast)': 'copy',
        'Re-encode Audio': 'encode',
        'No Audio (Mute)': 'disable',
    }

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.process: Optional[subprocess.Popen] = None
        self.export_thread = None
        self.cancelled = False

        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setModal(True)
        self.config = config.load_config()

        # Build common widget attributes
        self.mode = 'encode'
        self.resolution = 'Original'
        self.video_codec = self.config.get('export_video_codec', 'H.264 (libx264)')
        self.crf = self.config.get('export_crf', 23)
        self.audio_mode = self.config.get('export_audio_mode', 'Copy Audio (Fast)')
        self.audio_codec = self.config.get('export_audio_codec', 'AAC (aac)')
        self.audio_bitrate = self.config.get('export_audio_bitrate', '192k')
        self.open_folder = self.config.get('export_open_folder', True)
        self.custom_width = self.config.get('export_custom_width', 1920)
        self.custom_height = self.config.get('export_custom_height', 1080)
        self.container = self.config.get('export_container', 'MP4 (.mp4)')
        self.naming_template = self.config.get('naming_template', '{source-name}_scene_{time-start}')

    def _get_core_ffmpeg_args(self, metadata: dict) -> list:
        args = self._get_video_encode_args()
        args.extend(self._get_audio_args())
        args.extend(['-map', '0:v:0'])
        if metadata.get('has_audio'):
            args.extend(['-map', '0:a?'])
        args.extend(['-avoid_negative_ts', 'make_zero', '-y'])
        return args

    def _build_mode_section(self, parent):
        group = QGroupBox('Export Mode')
        layout = QVBoxLayout(group)

        self._mode_copy = QRadioButton('Stream Copy (Fast, Lossless)')
        self._mode_encode = QRadioButton('Re-encode (Exact Frame Accuracy)')

        if self.mode == 'copy':
            self._mode_copy.setChecked(True)
        else:
            self._mode_encode.setChecked(True)

        self._mode_copy.toggled.connect(self._update_widget_states)
        self._mode_encode.toggled.connect(self._update_widget_states)

        layout.addWidget(self._mode_copy)
        layout.addWidget(self._mode_encode)

        group.setToolTip(
            'Stream Copy cuts on keyframes only. The cut timing may not be exact.\n'
            'Re-encode mode provides exact frame accuracy but takes longer.'
        )
        parent.layout().addWidget(group)

    def _build_container_section(self, parent):
        group = QGroupBox('Container')
        layout = QHBoxLayout(group)
        
        layout.addWidget(QLabel('Format:'))
        self._container_combo = QComboBox()
        self._container_combo.addItems(list(self.CONTAINERS.keys()))
        idx = self._container_combo.findText(self.container)
        if idx >= 0:
            self._container_combo.setCurrentIndex(idx)
            
        self._container_combo.currentTextChanged.connect(self._update_preview_display)
        layout.addWidget(self._container_combo)
        layout.addStretch()
        
        parent.layout().addWidget(group)

    def _build_naming_section(self, parent, is_bulk=False):
        self.is_bulk = is_bulk
        group = QGroupBox('Output & Naming')
        layout = QVBoxLayout(group)

        path_layout = QHBoxLayout()
        self._output_dir_edit = QLineEdit()
        path_layout.addWidget(self._output_dir_edit)
        
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self._browse_output_dir if is_bulk else self._browse_output)
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)

        temp_layout = QHBoxLayout()
        temp_layout.addWidget(QLabel('Template:'))
        self._template_edit = QLineEdit(self.naming_template)
        self._template_edit.textChanged.connect(self._update_preview_display)
        temp_layout.addWidget(self._template_edit)

        self.tag_options = {
            "Original Name": "{source-name}", "Date": "{date-today}", "Scene ID": "{scene-id}",
            "Start": "{time-start}", "End": "{time-end}", "Codec": "{codec}", "Res": "{res}",
        }
        self._tag_selector = QComboBox()
        self._tag_selector.addItem("Insert Tag...")
        self._tag_selector.addItems(list(self.tag_options.keys()))
        self._tag_selector.activated.connect(self._on_tag_selected)
        temp_layout.addWidget(self._tag_selector)
        layout.addLayout(temp_layout)

        layout.addWidget(QLabel("Filename Preview:"))
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        self._preview_label.setStyleSheet("font-style: italic; color: gray;")
        layout.addWidget(self._preview_label)

        parent.layout().addWidget(group)

    def _on_tag_selected(self, index):
        if index <= 0: return
        tag_key = self._tag_selector.currentText()
        tag_val = self.tag_options.get(tag_key)
        if tag_val:
            # Insert tag at current cursor position
            self._template_edit.insert(tag_val)
        
        # Reset dropdown silently
        self._tag_selector.blockSignals(True)
        self._tag_selector.setCurrentIndex(0)
        self._tag_selector.blockSignals(False)

    def _resolve_naming_template(self, template: str, metadata: dict, video_path: str, start_ms: int, end_ms: int, scene_idx: int = 0) -> str:
        tags = {
            '{source-name}': os.path.splitext(os.path.basename(video_path))[0],
            '{scene-id}': str(scene_idx + 1),
            '{time-start}': f"{start_ms / 1000.0:.1f}s",
            '{time-end}': f"{end_ms / 1000.0:.1f}s",
            '{duration}': f"{(end_ms - start_ms) / 1000.0:.1f}s",
            '{codec}': metadata.get('video_codec', 'unknown') if metadata else 'unknown',
            '{res}': f"{metadata.get('width', 0)}x{metadata.get('height', 0)}" if metadata else "0x0",
            '{date-today}': date.today().isoformat(),
        }
        result = template
        for tag, value in tags.items():
            result = result.replace(tag, value)
        
        # Sanitize OS-invalid characters
        sanitized = re.sub(r'[*?:"<>|]', "_", result)
        return os.path.normpath(sanitized)

    def _update_preview_display(self):
        metadata, v_path, s_ms, e_ms = self._get_preview_params()
        template = self._template_edit.text() if hasattr(self, '_template_edit') else self.naming_template
        filename = self._resolve_naming_template(template, metadata, v_path, s_ms, e_ms)
        
        combo_text = self._container_combo.currentText() if hasattr(self, '_container_combo') else self.container
        ext = self.CONTAINERS.get(combo_text, '.mp4')
        full_name = f"{filename}{ext}"
        
        if hasattr(self, '_preview_label'):
            self._preview_label.setText(full_name)

        # For Single Exporter: Dynamically update the full file path string in the UI
        if not getattr(self, 'is_bulk', True) and hasattr(self, '_output_dir_edit'):
            current_path = self._output_dir_edit.text()
            folder = os.path.dirname(current_path) if current_path else os.path.dirname(v_path)
            new_path = os.path.join(folder, full_name)
            if current_path != new_path:
                self._output_dir_edit.setText(new_path)

    def _get_preview_params(self):
        # Override in subclasses
        return {}, 'video.mp4', 0, 10000

    def _browse_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose Bulk Export Folder', self._output_dir_edit.text() or os.getcwd())
        if path:
            self._output_dir_edit.setText(path)

    def _browse_output(self):
        initial = self._output_dir_edit.text()
        path, _ = QFileDialog.getSaveFileName(self, 'Export Scene As', initial, 'All Files (*.*)')
        if path:
            self._output_dir_edit.setText(path)

    def _build_video_options(self, parent):
        self._video_group = QGroupBox('Video Options')
        layout = QGridLayout(self._video_group)

        layout.addWidget(QLabel('Resolution:'), 0, 0)
        self._res_combo = QComboBox()
        self._res_combo.addItems(list(self.RESOLUTION_PRESETS.keys()))
        idx = self._res_combo.findText(self.resolution)
        if idx >= 0:
            self._res_combo.setCurrentIndex(idx)
        self._res_combo.currentTextChanged.connect(self._update_widget_states)
        layout.addWidget(self._res_combo, 0, 1)

        self._custom_res_frame = QWidget()
        custom_layout = QHBoxLayout(self._custom_res_frame)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        self._width_edit = QLineEdit(str(self.custom_width))
        self._width_edit.setFixedWidth(60)
        custom_layout.addWidget(self._width_edit)
        custom_layout.addWidget(QLabel('x'))
        self._height_edit = QLineEdit(str(self.custom_height))
        self._height_edit.setFixedWidth(60)
        custom_layout.addWidget(self._height_edit)
        self._custom_res_frame.setVisible(self.resolution == 'Custom')
        layout.addWidget(self._custom_res_frame, 0, 2)

        layout.addWidget(QLabel('Video Codec:'), 1, 0)
        self._codec_combo = QComboBox()
        self._codec_combo.addItems(list(self.VIDEO_CODECS.keys()))
        idx = self._codec_combo.findText(self.video_codec)
        if idx >= 0:
            self._codec_combo.setCurrentIndex(idx)
        layout.addWidget(self._codec_combo, 1, 1)

        layout.addWidget(QLabel('Quality (CRF):'), 2, 0)
        crf_frame = QWidget()
        crf_layout = QHBoxLayout(crf_frame)
        crf_layout.setContentsMargins(0, 0, 0, 0)
        self._crf_spin = QSpinBox()
        self._crf_spin.setRange(0, 51)
        self._crf_spin.setValue(self.crf)
        crf_layout.addWidget(self._crf_spin)
        crf_layout.addWidget(QLabel('(0=best, 51=worst, 23=default)'))
        layout.addWidget(crf_frame, 2, 1)

        parent.layout().addWidget(self._video_group)

    def _build_audio_options(self, parent):
        group = QGroupBox('Audio Options')
        layout = QGridLayout(group)

        layout.addWidget(QLabel('Audio Mode:'), 0, 0)
        self._audio_mode_combo = QComboBox()
        self._audio_mode_combo.addItems(list(self.AUDIO_MODES.keys()))
        idx = self._audio_mode_combo.findText(self.audio_mode)
        if idx >= 0:
            self._audio_mode_combo.setCurrentIndex(idx)
        self._audio_mode_combo.currentTextChanged.connect(self._update_widget_states)
        layout.addWidget(self._audio_mode_combo, 0, 1)

        layout.addWidget(QLabel('Audio Codec:'), 1, 0)
        self._audio_codec_combo = QComboBox()
        self._audio_codec_combo.addItems(list(self.AUDIO_CODECS.keys()))
        idx = self._audio_codec_combo.findText(self.audio_codec)
        if idx >= 0:
            self._audio_codec_combo.setCurrentIndex(idx)
        layout.addWidget(self._audio_codec_combo, 1, 1)

        layout.addWidget(QLabel('Audio Bitrate:'), 2, 0)
        self._audio_bitrate_combo = QComboBox()
        self._audio_bitrate_combo.addItems(['128k', '192k', '256k', '320k'])
        idx = self._audio_bitrate_combo.findText(self.audio_bitrate)
        if idx >= 0:
            self._audio_bitrate_combo.setCurrentIndex(idx)
        layout.addWidget(self._audio_bitrate_combo, 2, 1)

        parent.layout().addWidget(group)

    def _build_button_section(self, parent, export_text='Export'):
        self._open_folder_check = QCheckBox('Open folder after export')
        self._open_folder_check.setChecked(self.open_folder)
        parent.layout().addWidget(self._open_folder_check)

        btn_layout = QHBoxLayout()
        self._export_btn = QPushButton(export_text)
        self._export_btn.clicked.connect(self._start_export)
        btn_layout.addWidget(self._export_btn)

        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn)

        parent.layout().addLayout(btn_layout)

    def _update_widget_states(self):
        is_encode = self._mode_encode.isChecked()
        audio_mode = self._audio_mode_combo.currentText()

        self._res_combo.setEnabled(is_encode)
        self._codec_combo.setEnabled(is_encode)
        self._crf_spin.setEnabled(is_encode)

        is_reencode_audio = (audio_mode == 'Re-encode Audio')
        self._audio_codec_combo.setEnabled(is_reencode_audio)
        self._audio_bitrate_combo.setEnabled(is_reencode_audio)

        res = self._res_combo.currentText()
        self._custom_res_frame.setVisible(res == 'Custom' and is_encode)

        if hasattr(self, '_keyframe_info_label') and self._keyframe_info_label:
            self._keyframe_info_label.setText(self._get_keyframe_info())

    def _save_common_settings(self):
        self.config['export_mode'] = 'copy' if self._mode_copy.isChecked() else 'encode'
        self.config['export_resolution'] = self._res_combo.currentText()
        self.config['export_audio_mode'] = self._audio_mode_combo.currentText()
        self.config['export_video_codec'] = self._codec_combo.currentText()
        self.config['export_audio_codec'] = self._audio_codec_combo.currentText()
        self.config['export_crf'] = self._crf_spin.value()
        self.config['export_audio_bitrate'] = self._audio_bitrate_combo.currentText()
        self.config['export_open_folder'] = self._open_folder_check.isChecked()
        self.config['export_custom_width'] = int(self._width_edit.text())
        self.config['export_custom_height'] = int(self._height_edit.text())
        
        # New Settings to Save
        self.config['export_container'] = self._container_combo.currentText()
        self.config['naming_template'] = self._template_edit.text()

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
        codec_name = self.VIDEO_CODECS.get(self._codec_combo.currentText(), 'libx264')
        args.extend(['-c:v', codec_name, '-crf', str(self._crf_spin.value())])

        res_choice = self._res_combo.currentText()
        if res_choice == 'Custom':
            args.extend(['-vf', f'scale={self._width_edit.text()}:{self._height_edit.text()}'])
        else:
            target_height = self.RESOLUTION_PRESETS.get(res_choice)
            if target_height:
                args.extend(['-vf', f'scale=-2:{target_height}'])
        return args

    def _get_audio_args(self) -> list:
        audio_mode = self.AUDIO_MODES.get(self._audio_mode_combo.currentText())
        if audio_mode == 'disable':
            return ['-an']
        if audio_mode == 'copy':
            return ['-c:a', 'copy']
        codec_name = self.AUDIO_CODECS.get(self._audio_codec_combo.currentText(), 'aac')
        return ['-c:a', codec_name, '-b:a', self._audio_bitrate_combo.currentText()]

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


def get_video_info_and_keyframe(video_path: str, target_ms: int) -> Dict[str, Any]:
    info = {
        'width': 1920, 'height': 1080, 'framerate': 30.0,
        'has_audio': False, 'audio_codec': None, 'video_codec': None,
        'duration_ms': 0, 'keyframe_ms': target_ms, 'error': None,
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
    'H.264 (libx264)': 'libx264', 'H.265 (libx265)': 'libx265',
    'AV1 (libsvtav1)': 'libsvtav1', 'VP9 (libvpx-vp9)': 'libvpx-vp9',
    'ProRes 422 (prores_ks)': 'prores_ks',
}


def build_ffmpeg_args_headless(config_dict: dict, metadata: dict) -> list:
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
        '-ss', str(fast_seek), '-i', video_path,
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
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation_flags)
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
