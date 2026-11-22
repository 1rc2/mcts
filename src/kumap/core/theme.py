"""深色 / 浅色主题：一处配色，全局 QSS。"""

from __future__ import annotations

DARK = {
    "bg": "#171a1f",
    "bg2": "#1e2229",
    "panel": "#20252d",
    "panel2": "#262c36",
    "text": "#e6e9ef",
    "text2": "#9aa4b2",
    "accent": "#4fd1c5",
    "accent_text": "#0d1117",
    "border": "#333b47",
    "done": "#5b6472",
    "warn": "#e0a458",
    "error": "#e57373",
    "ok": "#7cc47f",
    "map_bg": "#101317",
    "titlebar": "#1b1f26",
}

LIGHT = {
    "bg": "#f4f6f9",
    "bg2": "#eaeef3",
    "panel": "#ffffff",
    "panel2": "#f0f3f7",
    "text": "#22282f",
    "text2": "#66707d",
    "accent": "#0f9b8e",
    "accent_text": "#ffffff",
    "border": "#d3dae3",
    "done": "#a9b2bd",
    "warn": "#b8791f",
    "error": "#c0392b",
    "ok": "#2e7d32",
    "map_bg": "#e6e9ee",
    "titlebar": "#e3e8ef",
}


def palette(theme: str) -> dict:
    return LIGHT if str(theme).lower() == "light" else DARK


def qss(theme: str) -> str:
    c = palette(theme)
    return f"""
QWidget {{
    background: {c['bg']};
    color: {c['text']};
    font-family: "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif;
    font-size: 13px;
}}
#TitleBar {{ background: {c['titlebar']}; }}
#TitleLabel {{ color: {c['text']}; font-size: 13px; font-weight: 600; }}
#StatusChip {{
    background: {c['panel2']};
    color: {c['text2']};
    border: 1px solid {c['border']};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
#WarnChip {{
    background: {c['panel2']};
    color: {c['warn']};
    border: 1px solid {c['border']};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
QToolButton, QPushButton {{
    background: {c['panel2']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px 9px;
}}
QToolButton:hover, QPushButton:hover {{ background: {c['border']}; }}
QToolButton:checked, QPushButton:checked {{
    background: {c['accent']};
    color: {c['accent_text']};
    border-color: {c['accent']};
}}
QToolButton:disabled, QPushButton:disabled {{ color: {c['text2']}; background: {c['bg2']}; }}
#Panel {{ background: {c['panel']}; border-left: 1px solid {c['border']}; }}
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 6px;
    font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['text2']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {c['bg2']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px 6px;
    color: {c['text']};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
QListWidget, QTreeWidget, QListWidget#RouteList {{
    background: {c['bg2']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QListWidget::item:hover {{ background: {c['panel2']}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid {c['border']}; background: {c['bg2']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QRadioButton::indicator {{ width: 13px; height: 13px; border-radius: 7px; border: 1px solid {c['border']}; background: {c['bg2']}; }}
QRadioButton::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 13px; margin: -5px 0; border-radius: 6px; background: {c['accent']}; }}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 24px; }}
QTabBar::tab {{ background: {c['bg2']}; padding: 5px 12px; border: 1px solid {c['border']}; border-bottom: none; border-radius: 6px 6px 0 0; }}
QTabBar::tab:selected {{ background: {c['panel']}; color: {c['accent']}; }}
QStatusBar, #StatusBar {{ background: {c['bg2']}; color: {c['text2']}; border-top: 1px solid {c['border']}; font-size: 11px; }}
#Disclaimer {{ color: {c['text2']}; font-size: 11px; }}
#ProgressValue {{ color: {c['accent']}; font-weight: 600; }}
QToolTip {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    padding: 6px 8px;
    font-size: 12px;
}}
QMenu {{ background: {c['panel']}; border: 1px solid {c['border']}; }}
QMenu::item {{ padding: 5px 22px; }}
QMenu::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
"""
