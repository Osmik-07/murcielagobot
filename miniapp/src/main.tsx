import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppRoot } from '@telegram-apps/telegram-ui';
import { TonConnectUIProvider } from '@tonconnect/ui-react';

import '@telegram-apps/telegram-ui/dist/styles.css';
import './theme.css';

import { App } from './App';
import { colorScheme, initTelegram } from './telegram';

initTelegram();

// Манифест лежит на том же домене, что и приложение: кошелёк показывает
// по нему, какое приложение просит подключение.
const MANIFEST_URL = `${window.location.origin}/tonconnect-manifest.json`;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <TonConnectUIProvider manifestUrl={MANIFEST_URL}>
      {/* appearance берём из темы клиента — приложение не навязывает свою */}
      <AppRoot appearance={colorScheme()}>
        <App />
      </AppRoot>
    </TonConnectUIProvider>
  </StrictMode>,
);
