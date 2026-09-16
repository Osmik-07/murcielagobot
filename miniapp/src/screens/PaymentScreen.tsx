import { useEffect, useState } from 'react';
import { Cell, List, Radio, Section, Spinner } from '@telegram-apps/telegram-ui';
import {
  useTonAddress,
  useTonConnectUI,
  useTonWallet,
  type WalletInfo,
} from '@tonconnect/ui-react';

import { api, ApiError, type Asset, type Order, type PriceEntry, type Product } from '../api';
import { approxAmount, months, starsCount } from '../format';
import { PremiumIcon, StarIcon, TonLogo, UsdtLogo, WalletIcon } from '../icons';
import { haptic, mainButton } from '../telegram';

/**
 * Кошельки, которые показываем списком с логотипами — в этом порядке.
 * Wallet первым: он встроен в Telegram, и для большинства покупателей это
 * единственный знакомый кошелёк. Остальные — через «Другой кошелёк».
 * appName — идентификаторы из официального реестра TON Connect.
 */
const FEATURED_WALLETS = ['telegram-wallet', 'tonkeeper'] as const;

/**
 * Подписи под названием. Названия и логотипы берём из реестра как есть,
 * а подпись — наша: реестр называет tonkeeper «Keeper» (кошелёк сменил имя),
 * а многие знают его по старому — так его проще узнать в списке.
 */
const WALLET_SUBTITLE: Record<string, string> = {
  'telegram-wallet': 'Встроен в Telegram',
  tonkeeper: 'Tonkeeper',
};

function Chevron() {
  return (
    <svg width="8" height="14" viewBox="0 0 8 14" fill="none" style={{ color: 'var(--tgui--hint_color)' }}>
      <path d="M1 1l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

interface Props {
  product: Product;
  months?: number;
  stars?: number;
  recipient: string;
  onPaid: (order: Order) => void;
}

/**
 * Выбор валюты и кошелька, отправка перевода.
 *
 * Этот экран НЕ решает, оплачен ли заказ, — он только помогает отправить
 * перевод. Оплату подтверждает сервер, когда TonConsole реально увидит деньги,
 * поэтому даже ложный «успех» от кошелька не выдаст товар раньше времени.
 */
export function PaymentScreen({ product, months: period, stars, recipient, onPaid }: Props) {
  const [asset, setAsset] = useState<Asset>('ton');
  const [prices, setPrices] = useState<Record<Asset, PriceEntry> | null>(null);
  const [wallets, setWallets] = useState<WalletInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [tonConnectUI] = useTonConnectUI();
  const wallet = useTonWallet();
  const address = useTonAddress();

  const title = product === 'premium' ? `Premium · ${months(period!)}` : `${starsCount(stars!)} Stars`;

  useEffect(() => {
    let cancelled = false;
    api
      .price(product === 'premium' ? { product, months: period } : { product, stars })
      .then((r) => !cancelled && setPrices(r.prices))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [product, period, stars]);

  // Список кошельков и их логотипы — из официального реестра TON Connect,
  // а не картинками у нас: так логотип всегда актуальный и настоящий.
  useEffect(() => {
    let cancelled = false;
    tonConnectUI
      .getWallets()
      .then((all) => {
        if (cancelled) return;
        const featured = FEATURED_WALLETS.map((name) => all.find((w) => w.appName === name)).filter(
          (w): w is WalletInfo => Boolean(w),
        );
        setWallets(featured);
      })
      .catch(() => !cancelled && setWallets([]));
    return () => {
      cancelled = true;
    };
  }, [tonConnectUI]);

  async function connect(appName: string | null) {
    haptic('tap');
    setError(null);
    try {
      if (wallet) await tonConnectUI.disconnect();
      if (appName) await tonConnectUI.openSingleWalletModal(appName);
      else await tonConnectUI.openModal();
    } catch {
      // openSingleWalletModal помечен в библиотеке как экспериментальный —
      // если не сработал, показываем общий список, а не молча ничего.
      await tonConnectUI.openModal();
    }
  }

  async function pay() {
    if (busy || !wallet) return;
    setBusy(true);
    setError(null);
    let order: Order | null = null;
    try {
      ({ order } = await api.createOrder({
        product,
        months: period,
        stars,
        asset,
        recipient,
      }));
      const tx = await api.orderTx(order.id, address);
      await tonConnectUI.sendTransaction({ validUntil: tx.valid_until, messages: tx.messages });
      haptic('success');
      onPaid(order);
    } catch (e) {
      // По прошлому заказу уже пришли деньги — ведём на его статус.
      if (e instanceof ApiError && e.order) {
        onPaid(e.order);
        return;
      }
      const message = e instanceof Error ? e.message : String(e);
      // Отказ в кошельке — не ошибка: человек передумал, заказ подождёт.
      if (!/reject|cancel|declin/i.test(message)) {
        haptic('error');
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  }

  const current = prices?.[asset];

  useEffect(
    () =>
      mainButton({
        text: !wallet
          ? 'Выбери кошелёк'
          : current
            ? `Оплатить ${approxAmount(current.approx, asset)}`
            : 'Оплатить',
        loading: busy,
        disabled: !wallet || !current,
        onClick: pay,
      }),
    // pay замыкает актуальное состояние — переустанавливаем кнопку на его изменение
    [wallet, busy, current, asset, address, recipient, product, period, stars],
  );

  const connectedInfo = wallet
    ? wallets?.find((w) => w.appName === wallet.device.appName)
    : undefined;

  return (
    <List>
      <Section header="Заказ">
        <Cell
          before={product === 'premium' ? <PremiumIcon size={40} /> : <StarIcon size={40} boxed />}
          subtitle={`для @${recipient}`}
        >
          {title}
        </Cell>
      </Section>

      <Section header="Валюта">
        {(['ton', 'usdt_ton'] as const).map((a) => (
          <Cell
            key={a}
            Component="label"
            before={a === 'ton' ? <TonLogo /> : <UsdtLogo />}
            subtitle={a === 'ton' ? 'Toncoin' : 'Tether, сеть TON'}
            after={
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                {prices ? (
                  <span style={{ color: 'var(--tgui--hint_color)' }}>
                    {approxAmount(prices[a].approx, a)}
                  </span>
                ) : (
                  <Spinner size="s" />
                )}
                <Radio
                  name="asset"
                  checked={asset === a}
                  onChange={() => {
                    haptic('tap');
                    setAsset(a);
                  }}
                />
              </div>
            }
          >
            {a === 'ton' ? 'TON' : 'USDT'}
          </Cell>
        ))}
      </Section>

      <Section
        header="Кошелёк"
        footer={
          wallet
            ? 'Нажми на кошелёк, чтобы подключить другой.'
            : 'Кошелёк откроется, чтобы подтвердить перевод. Ключи остаются у тебя.'
        }
      >
        {wallet ? (
          <Cell
            before={
              connectedInfo ? (
                <img className="wallet-logo" src={connectedInfo.imageUrl} alt="" />
              ) : (
                <WalletIcon size={40} />
              )
            }
            subtitle={`${address.slice(0, 6)}…${address.slice(-6)}`}
            after={<span style={{ color: 'var(--tgui--link_color)' }}>Сменить</span>}
            onClick={() => connect(null)}
          >
            {connectedInfo?.name ?? 'Кошелёк подключён'}
          </Cell>
        ) : wallets === null ? (
          <Cell before={<Spinner size="m" />}>Загружаем кошельки…</Cell>
        ) : (
          // Массивом, а не фрагментом: Section ставит разделители между
          // своими прямыми детьми, а фрагмент для неё — один ребёнок.
          [
            ...wallets.map((w) => (
              <Cell
                key={w.appName}
                before={<img className="wallet-logo" src={w.imageUrl} alt="" />}
                subtitle={WALLET_SUBTITLE[w.appName]}
                after={<Chevron />}
                onClick={() => connect(w.appName)}
              >
                {w.name}
              </Cell>
            )),
            <Cell
              key="other"
              before={
                <div
                  className="wallet-logo"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'var(--tgui--secondary_bg_color)',
                    color: 'var(--tgui--link_color)',
                  }}
                >
                  <WalletIcon size={22} />
                </div>
              }
              subtitle="MyTonWallet и другие"
              after={<Chevron />}
              onClick={() => connect(null)}
            >
              Другой кошелёк
            </Cell>,
          ]
        )}
      </Section>

      {current && (
        <div className="amount-hero">
          <div style={{ fontSize: 14, color: 'var(--tgui--hint_color)', marginBottom: 6 }}>
            К оплате
          </div>
          <div className="amount-hero__value">≈ {approxAmount(current.approx, asset).replace(/\s\S+$/, '')}</div>
          <div className="amount-hero__asset">{asset === 'ton' ? 'TON' : 'USDT'}</div>
          <div style={{ fontSize: 13, color: 'var(--tgui--hint_color)', marginTop: 10 }}>
            Точная сумма будет в кошельке — по ней мы находим платёж.
          </div>
        </div>
      )}

      {error && (
        <Section>
          <Cell multiline style={{ color: 'var(--tgui--destructive_text_color)' }}>
            {error}
          </Cell>
        </Section>
      )}
    </List>
  );
}
