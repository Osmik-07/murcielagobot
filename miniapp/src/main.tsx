import { StrictMode, useEffect, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppRoot } from '@telegram-apps/telegram-ui';
import { TonConnectUIProvider } from '@tonconnect/ui-react';

import '@telegram-apps/telegram-ui/dist/styles.css';
import './theme.css';

import { App } from './App';
import { initTelegram, onThemeChange, platform, syncTheme } from './telegram';

initTelegram();
// Тема — до первого рендера, чтобы не мигнуть чужими цветами.
const initialAppearance = syncTheme();

/** Корень с темой: пересобирает палитру, когда пользователь меняет тему в Telegram. */
function ThemedRoot({ children }: { children: ReactNode }) {
  const [appearance, setAppearance] = useState(initialAppearance);
  useEffect(() => onThemeChange(() => setAppearance(syncTheme())), []);
  return (
    <AppRoot
      appearance={appearance}
      platform={platform()}
      // AppRoot сам красит всю страницу: переменные --tgui--* объявлены
      // именно на нём, поэтому и фон, и текст внутри гарантированно из одной темы.
      style={{ minHeight: '100vh', background: 'var(--tgui--secondary_bg_color)', color: 'var(--tgui--text_color)' }}
    >
      {children}
    </AppRoot>
  );
}

// Манифест отдаёт бэкенд на корне того же домена: кошелёк показывает по нему,
// какое приложение просит подключение.
const MANIFEST_URL = `${window.location.origin}/tonconnect-manifest.json`;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <TonConnectUIProvider
      manifestUrl={MANIFEST_URL}
      // После подтверждения в кошельке возвращаемся обратно в Telegram,
      // а не остаёмся во внешнем приложении.
      actionsConfiguration={{ twaReturnUrl: 'https://t.me/murcielago_nebot' }}
    >
      <ThemedRoot>
        <App />
      </ThemedRoot>
    </TonConnectUIProvider>
  </StrictMode>,
);
