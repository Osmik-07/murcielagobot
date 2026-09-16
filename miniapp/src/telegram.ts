/**
 * Тонкая типизированная обёртка над window.Telegram.WebApp.
 *
 * Почему не SDK-обёртка из npm: этот объект — собственный стабильный API
 * Telegram, он приходит вместе с telegram-web-app.js и не меняется годами.
 * Лишняя зависимость здесь дала бы только свой цикл релизов и свои поломки.
 *
 * Всё, что тут есть, может отсутствовать: приложение должно открываться и
 * в обычном браузере (например, когда мы сами проверяем вёрстку), просто
 * без нативных кнопок.
 */

import { applyPalette, buildPalette, type Appearance } from './theme';

interface TelegramWebApp {
  initData: string;
  initDataUnsafe?: { user?: { id: number; username?: string; first_name?: string } };
  colorScheme: 'light' | 'dark';
  themeParams: Record<string, string>;
  platform: string;
  ready(): void;
  expand(): void;
  close(): void;
  // Есть не во всех версиях клиента — вызываем только после проверки.
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  setBottomBarColor?(color: string): void;
  onEvent?(event: 'themeChanged', cb: () => void): void;
  offEvent?(event: 'themeChanged', cb: () => void): void;
  MainButton: {
    setText(text: string): void;
    show(): void;
    hide(): void;
    enable(): void;
    disable(): void;
    showProgress(leaveActive?: boolean): void;
    hideProgress(): void;
    onClick(cb: () => void): void;
    offClick(cb: () => void): void;
    setParams(params: { color?: string; text_color?: string }): void;
  };
  BackButton: {
    show(): void;
    hide(): void;
    onClick(cb: () => void): void;
    offClick(cb: () => void): void;
  };
  HapticFeedback?: {
    impactOccurred(style: 'light' | 'medium' | 'heavy'): void;
    notificationOccurred(type: 'error' | 'success' | 'warning'): void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export const tg = (): TelegramWebApp | undefined => window.Telegram?.WebApp;

/** Строка initData, которой бэкенд подтверждает, кто мы. Пустая вне Telegram. */
export const initData = (): string => tg()?.initData ?? '';

/**
 * Применяет тему Telegram (с достройкой недостающих цветов, см. theme.ts)
 * и возвращает схему, которой должны рисоваться компоненты.
 */
export function syncTheme(): Appearance {
  const app = tg();
  const fallback: Appearance =
    app?.colorScheme ?? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const { appearance, palette } = buildPalette(app?.themeParams ?? {}, fallback);
  applyPalette(palette, appearance);

  // Шапка и нижняя панель Telegram — в цвет фона страницы, иначе вокруг
  // приложения остаётся полоса другого цвета и оно выглядит «вставленным».
  // Передаём готовый hex, а не ключ темы: ключа у клиента может и не быть.
  try {
    app?.setHeaderColor?.(palette.secondary_bg_color);
    app?.setBackgroundColor?.(palette.secondary_bg_color);
    app?.setBottomBarColor?.(palette.secondary_bg_color);
  } catch {
    // Старый клиент без этих методов — не критично, просто без подкраски.
  }
  return appearance;
}

/** Подписка на смену темы в Telegram (пользователь переключил ночной режим). */
export function onThemeChange(cb: () => void): () => void {
  const app = tg();
  if (!app?.onEvent) return () => {};
  app.onEvent('themeChanged', cb);
  return () => app.offEvent?.('themeChanged', cb);
}

/**
 * С каким товаром открыли приложение: кнопки в боте передают
 * ?startapp=premium | stars. Если параметра нет — покажем выбор.
 */
export function startProduct(): 'premium' | 'stars' | null {
  const params = new URLSearchParams(window.location.search);
  // tgWebAppStartParam — когда приложение открыто ссылкой t.me/bot/app?startapp=…
  // product — когда кнопкой web_app из бота (там URL уходит как есть).
  const raw = (params.get('tgWebAppStartParam') ?? params.get('product') ?? '').toLowerCase();
  return raw === 'premium' || raw === 'stars' ? raw : null;
}

export function haptic(kind: 'tap' | 'success' | 'error'): void {
  const h = tg()?.HapticFeedback;
  if (!h) return;
  if (kind === 'tap') h.impactOccurred('light');
  else h.notificationOccurred(kind === 'success' ? 'success' : 'error');
}

/** Нативная кнопка внизу экрана. Возвращает функцию отписки. */
export function mainButton(opts: {
  text: string;
  onClick: () => void;
  loading?: boolean;
  disabled?: boolean;
}): () => void {
  const app = tg();
  if (!app) return () => {};
  const { MainButton } = app;
  MainButton.setText(opts.text);
  if (opts.disabled) MainButton.disable();
  else MainButton.enable();
  if (opts.loading) MainButton.showProgress(true);
  else MainButton.hideProgress();
  MainButton.onClick(opts.onClick);
  MainButton.show();
  return () => {
    MainButton.offClick(opts.onClick);
    MainButton.hide();
  };
}

/** Нативная стрелка «назад» в шапке. Возвращает функцию отписки. */
export function backButton(onClick: () => void): () => void {
  const app = tg();
  if (!app) return () => {};
  app.BackButton.onClick(onClick);
  app.BackButton.show();
  return () => {
    app.BackButton.offClick(onClick);
    app.BackButton.hide();
  };
}

/** Вид компонентов: на iOS — iOS-стиль, иначе базовый (Android/desktop). */
export const platform = (): 'ios' | 'base' => {
  const p = tg()?.platform ?? '';
  return p === 'ios' || p === 'macos' ? 'ios' : 'base';
};

export function initTelegram(): void {
  const app = tg();
  if (!app) return;
  app.ready();
  app.expand();
}
