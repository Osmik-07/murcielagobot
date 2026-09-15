import { useEffect, useMemo, useState } from 'react';
import { Cell, List, Section, Spinner } from '@telegram-apps/telegram-ui';
import { useTonAddress, useTonConnectModal, useTonConnectUI, useTonWallet } from '@tonconnect/ui-react';

import { api, type Asset, type Order, type PriceEntry, type Product } from '../api';
import { haptic, mainButton } from '../telegram';
import { StarIcon, PremiumIcon, WalletIcon } from '../icons';

interface Props {
  product: Product;
  months?: number;
  stars?: number;
  recipient: string;
  onPaid: (order: Order) => void;
}

/**
 * Выбор актива, подключение кошелька и отправка перевода.
 *
 * Важное про деньги: этот экран НЕ решает, оплачен ли заказ. Он только
 * помогает отправить перевод. Оплату подтверждает бэкенд, когда TonConsole
 * реально увидит деньги — поэтому даже если кошелёк соврёт об успехе,
 * товар не выдастся раньше времени.
 */
export function PaymentScreen({ product, months, stars, recipient, onPaid }: Props) {
  const [asset, setAsset] = useState<Asset>('ton');
  const [prices, setPrices] = useState<Record<Asset, PriceEntry> | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [tonConnectUI] = useTonConnectUI();
  const wallet = useTonWallet();
  const address = useTonAddress();
  const { open: openWalletModal } = useTonConnectModal();

  const summary = useMemo(
    () => (product === 'premium' ? `Premium на ${months} мес.` : `${stars} Stars`),
    [product, months, stars],
  );

  useEffect(() => {
    let cancelled = false;
    api
      .price(product === 'premium' ? { product, months } : { product, stars })
      .then((r) => !cancelled && setPrices(r.prices))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [product, months, stars]);

  /**
   * Заказ создаётся только в момент оплаты, а не при входе на экран: иначе
   * каждое открытие экрана плодило бы счета, которые никто не оплатит.
   */
  async function pay() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (!wallet) {
        openWalletModal();
        return;
      }

      const { order: created } = await api.createOrder({
        product,
        months,
        stars,
        asset,
        recipient,
      });
      setOrder(created);

      const tx = await api.orderTx(created.id, address);
      await tonConnectUI.sendTransaction({
        validUntil: tx.valid_until,
        messages: tx.messages,
      });

      haptic('success');
      onPaid(created);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      // Отказ в кошельке — это не ошибка, человек просто передумал.
      const rejected = /reject|cancel|decline/i.test(message);
      if (!rejected) {
        haptic('error');
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  }

  useEffect(
    () =>
      mainButton({
        text: wallet ? 'Оплатить' : 'Подключить кошелёк',
        loading: busy,
        disabled: !prices,
        onClick: pay,
      }),
    // pay замыкает актуальные состояния, поэтому переустанавливаем кнопку на их изменение
    [wallet, busy, prices, asset, recipient, product, months, stars, address],
  );

  const current = prices?.[asset];

  return (
    <List>
      <div style={{ padding: '20px 22px 12px', fontSize: 22, fontWeight: 700 }}>Оплата</div>

      <Section header="Заказ">
        <Cell before={product === 'premium' ? <PremiumIcon size={24} /> : <StarIcon size={24} boxed />}>
          {summary}
        </Cell>
        <Cell after={<span style={{ fontWeight: 500 }}>@{recipient}</span>}>Получатель</Cell>
      </Section>

      <Section header="Способ оплаты">
        <div style={{ display: 'flex', gap: 8, padding: '10px 16px' }}>
          {(['ton', 'usdt_ton'] as const).map((a) => (
            <button
              key={a}
              onClick={() => {
                haptic('tap');
                setAsset(a);
              }}
              style={{
                flex: 1,
                padding: '12px 0',
                borderRadius: 10,
                border: 'none',
                cursor: 'pointer',
                fontSize: 15,
                fontWeight: asset === a ? 700 : 500,
                background:
                  asset === a ? 'var(--tgui--secondary_fill)' : 'var(--tgui--secondary_bg_color)',
                color: asset === a ? 'var(--tgui--text_color)' : 'var(--tgui--hint_color)',
              }}
            >
              {a === 'ton' ? 'TON' : 'USDT (TON)'}
            </button>
          ))}
        </div>
      </Section>

      <Section header="Кошелёк">
        <Cell
          before={<WalletIcon />}
          subtitle={
            wallet
              ? `${address.slice(0, 6)}…${address.slice(-4)}`
              : 'Wallet в Telegram, Tonkeeper и другие'
          }
          onClick={() => {
            haptic('tap');
            if (!wallet) openWalletModal();
            else void tonConnectUI.disconnect();
          }}
        >
          {wallet ? 'Кошелёк подключён' : 'Подключить кошелёк'}
        </Cell>
      </Section>

      <div style={{ textAlign: 'center', padding: '18px 22px 8px' }}>
        <div style={{ fontSize: 13, opacity: 0.6, marginBottom: 4 }}>К оплате</div>
        {current ? (
          <>
            <div style={{ fontSize: 38, fontWeight: 700, letterSpacing: '-0.01em' }}>
              {current.approx_formatted.replace(/\s\S+$/, '')}
            </div>
            <div style={{ fontSize: 16, fontWeight: 600, opacity: 0.8, marginTop: 2 }}>
              {current.label}
            </div>
            <div style={{ fontSize: 12, opacity: 0.5, marginTop: 8 }}>
              Точная сумма к переводу появится в кошельке — по ней мы и находим платёж.
            </div>
          </>
        ) : (
          <Spinner size="m" />
        )}
      </div>

      {error && (
        <Section>
          <Cell multiline style={{ color: 'var(--tgui--destructive_text_color)' }}>
            {error}
          </Cell>
        </Section>
      )}

      {order && !error && (
        <div style={{ textAlign: 'center', fontSize: 13, opacity: 0.5, paddingBottom: 12 }}>
          Заказ создан, ждём подтверждение в кошельке
        </div>
      )}
    </List>
  );
}
