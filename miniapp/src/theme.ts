/**
 * Палитра приложения из темы Telegram — с защитой от неполной темы.
 *
 * Компоненты telegram-ui красятся переменными --tg-theme-*, а если какой-то
 * нет — берут запасной цвет своей светлой или тёмной схемы. Клиенты Telegram
 * передают разный набор цветов, и если, скажем, тёмная тема пришла без
 * secondary_bg_color, фон становится светлым запасным, а текст остаётся
 * белым из темы — текст сливается с фоном.
 *
 * Поэтому мы сами собираем ПОЛНУЮ палитру: всё, что прислал Telegram и что
 * согласуется со схемой, берём как есть; недостающее или несовместимое
 * (светлая поверхность в тёмной теме, текст без контраста) заменяем
 * стандартными цветами Telegram для этой схемы — и кладём все переменные на
 * :root. Так ни один компонент не может упасть в «чужой» запасной цвет.
 */

export type Appearance = 'light' | 'dark';

type Palette = Record<string, string>;

// Стандартные цвета Telegram iOS — запасные значения для каждой схемы.
const DEFAULTS: Record<Appearance, Palette> = {
  light: {
    bg_color: '#ffffff',
    secondary_bg_color: '#efeff4',
    section_bg_color: '#ffffff',
    header_bg_color: '#f8f8f8',
    bottom_bar_bg_color: '#f8f8f8',
    text_color: '#000000',
    hint_color: '#8e8e93',
    subtitle_text_color: '#8e8e93',
    section_header_text_color: '#6d6d72',
    link_color: '#007aff',
    accent_text_color: '#007aff',
    button_color: '#007aff',
    button_text_color: '#ffffff',
    destructive_text_color: '#ff3b30',
    section_separator_color: '#c8c7cc',
  },
  dark: {
    bg_color: '#000000',
    secondary_bg_color: '#1c1c1d',
    section_bg_color: '#2c2c2e',
    header_bg_color: '#1a1a1a',
    bottom_bar_bg_color: '#1d1d1d',
    text_color: '#ffffff',
    hint_color: '#98989e',
    subtitle_text_color: '#98989e',
    section_header_text_color: '#8d8e93',
    link_color: '#3e88f7',
    accent_text_color: '#3e88f7',
    button_color: '#3e88f7',
    button_text_color: '#ffffff',
    destructive_text_color: '#eb5545',
    section_separator_color: '#545458',
  },
};

const SURFACES = ['bg_color', 'secondary_bg_color', 'section_bg_color', 'header_bg_color', 'bottom_bar_bg_color'];
// Текст, который лежит на поверхностях, и минимальный контраст для него.
const FOREGROUNDS: Record<string, number> = {
  text_color: 4.5,
  hint_color: 2.5,
  subtitle_text_color: 2.5,
  section_header_text_color: 2.5,
  link_color: 2.5,
  accent_text_color: 2.5,
  destructive_text_color: 2.5,
};

function rgb(hex: string | undefined): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex ?? '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function luminance(hex: string): number {
  const c = rgb(hex)!.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const isDark = (hex: string) => luminance(hex) < 0.2;

/**
 * Схема определяется по реальным цветам, а не по флагу colorScheme: флаг и
 * присланные цвета у некоторых клиентов расходятся, а рисуем мы цветами.
 */
function detectAppearance(params: Palette, fallback: Appearance): Appearance {
  const bg = rgb(params.bg_color) ? params.bg_color : rgb(params.secondary_bg_color) ? params.secondary_bg_color : null;
  if (bg) return isDark(bg) ? 'dark' : 'light';
  if (rgb(params.text_color)) return isDark(params.text_color) ? 'light' : 'dark';
  return fallback;
}

export function buildPalette(params: Palette, fallback: Appearance): { appearance: Appearance; palette: Palette } {
  const appearance = detectAppearance(params, fallback);
  const defaults = DEFAULTS[appearance];
  const palette: Palette = { ...defaults };

  for (const key of Object.keys(params)) {
    if (rgb(params[key])) palette[key] = params[key];
  }

  // Поверхность не той «яркости», что схема, — заменяем.
  for (const key of SURFACES) {
    if (isDark(palette[key]) !== (appearance === 'dark')) palette[key] = defaults[key];
  }

  // Текст должен читаться и на фоне страницы, и на карточках.
  for (const [key, min] of Object.entries(FOREGROUNDS)) {
    const readable = ['secondary_bg_color', 'section_bg_color'].every(
      (surface) => contrast(palette[key], palette[surface]) >= min,
    );
    if (!readable) palette[key] = defaults[key];
  }

  if (contrast(palette.button_text_color, palette.button_color) < 2.5) {
    palette.button_text_color = luminance(palette.button_color) > 0.4 ? '#000000' : '#ffffff';
  }

  return { appearance, palette };
}

export function applyPalette(palette: Palette, appearance: Appearance): void {
  const root = document.documentElement;
  for (const [key, value] of Object.entries(palette)) {
    root.style.setProperty(`--tg-theme-${key.replace(/_/g, '-')}`, value);
  }
  root.style.colorScheme = appearance;
  root.dataset.appearance = appearance;
}
