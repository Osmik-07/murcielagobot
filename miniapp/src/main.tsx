import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppRoot } from '@telegram-apps/telegram-ui';
import { TonConnectUIProvider } from '@tonconnect/ui-react';

import '@telegram-apps/telegram-ui/dist/styles.css';
import './theme.css';

import { App } from './App';
import { colorScheme, initTelegram, platform } from './telegram';

initTelegram();

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
      <AppRoot
        appearance={colorScheme()}
        platform={platform()}
        // AppRoot сам красит всю страницу: переменные --tgui--* объявлены
        // именно на нём, поэтому и фон, и текст внутри гарантированно из одной темы.
        style={{ minHeight: '100vh', background: 'var(--tgui--secondary_bg_color)' }}
      >
        <App />
      </AppRoot>
    </TonConnectUIProvider>
  </StrictMode>,
);
