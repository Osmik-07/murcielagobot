import { useEffect, useState } from 'react';
import { Cell, List, Section, Spinner, Badge } from '@telegram-apps/telegram-ui';

import { api, type PriceEntry, type Product } from '../api';
import { haptic, mainButton } from '../telegram';
import { PremiumIcon, StarIcon } from '../icons';

const MONTHS = [3, 6, 12] as const;
const STAR_PRESETS = [50, 100, 500, 1000] as const;

type Prices = Record<number, Record<string, PriceEntry>>;

interface Props {
  product: Product;
  onNext: (choice: { months?: number; stars?: number }) => void;
}

/**
 * Выбор срока (Premium) или количества (Stars).
 *
 * Цены тянутся с сервера по каждому варианту — никаких зашитых чисел на
 * клиенте: цена Fragment живая и меняется.
 */
export function ProductScreen({ product, onNext }: Props) {
  const isPremium = product === 'premium';
  const [selected, setSelected] = useState<number>(isPremium ? 12 : 500);
  const [prices, setPrices] = useState<Prices>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Premium: грузим цены сразу по всем трём срокам, чтобы показать их списком.
  // Stars: только по выбранному количеству — вариантов слишком много.
  useEffect(() => {
    let cancelled = false;
    const wanted = isPremium ? [...MONTHS] : [selected];

    setLoading(true);
    setError(null);
    Promise.all(
      wanted.map((value) =>
        api
          .price(isPremium ? { product, months: value } : { product, stars: value })
          .then((r) => [value, r.prices] as const),
      ),
    )
      .then((entries) => {
        if (cancelled) return;
        setPrices((prev) => ({ ...prev, ...Object.fromEntries(entries) }));
      })
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));

    return () => {
      cancelled = true;
    };
  }, [product, isPremium, selected]);

  useEffect(
    () =>
      mainButton({
        text: 'Продолжить',
        disabled: loading || !!error,
        onClick: () => {
          haptic('tap');
          onNext(isPremium ? { months: selected } : { stars: selected });
        },
      }),
    [loading, error, selected, isPremium, onNext],
  );

  const priceLine = (value: number) => {
    const entry = prices[value];
    if (!entry) return <Spinner size="s" />;
    return (
      <div style={{ textAlign: 'right' }}>
        <div style={{ fontWeight: 600 }}>{entry.ton.approx_formatted}</div>
        <div style={{ fontSize: 13, opacity: 0.6 }}>≈ {entry.usdt_ton.approx_formatted}</div>
      </div>
    );
  };

  if (error) {
    return (
      <List>
        <Section header="Не получилось">
          <Cell multiline description={error}>
            Цена недоступна
          </Cell>
        </Section>
      </List>
    );
  }

  return (
    <List>
      <div style={{ padding: '20px 22px 4px', display: 'flex', alignItems: 'center', gap: 10 }}>
        {isPremium ? <PremiumIcon /> : <StarIcon size={32} boxed />}
        <div style={{ fontSize: 22, fontWeight: 700 }}>
          {isPremium ? 'Telegram Premium' : 'Telegram Stars'}
        </div>
      </div>
      <div style={{ padding: '0 22px 8px', fontSize: 15, opacity: 0.6 }}>
        Напрямую через Fragment, без посредников.
      </div>

      {isPremium ? (
        <Section header="Выбери срок">
          {MONTHS.map((m) => (
            <Cell
              key={m}
              Component="label"
              onClick={() => {
                haptic('tap');
                setSelected(m);
              }}
              before={<Radio checked={selected === m} />}
              after={priceLine(m)}
              titleBadge={m === 12 ? <Badge type="number">−15%</Badge> : undefined}
            >
              {m} мес.
            </Cell>
          ))}
        </Section>
      ) : (
        <>
          <Stepper value={selected} onChange={setSelected} />
          <Section header="Быстрый выбор">
            <div style={{ display: 'flex', gap: 8, padding: '8px 16px 12px' }}>
              {STAR_PRESETS.map((p) => (
                <button
                  key={p}
                  onClick={() => {
                    haptic('tap');
                    setSelected(p);
                  }}
                  style={{
                    flex: 1,
                    padding: '10px 0',
                    borderRadius: 10,
                    border: selected === p ? '1.5px solid var(--tgui--link_color)' : '1.5px solid transparent',
                    background: 'var(--tgui--secondary_bg_color)',
                    color: selected === p ? 'var(--tgui--link_color)' : 'var(--tgui--hint_color)',
                    fontWeight: selected === p ? 700 : 500,
                    fontSize: 15,
                    cursor: 'pointer',
                  }}
                >
                  {p}
                </button>
              ))}
            </div>
          </Section>
          <div style={{ textAlign: 'center', padding: '0 22px 12px', fontSize: 15, opacity: 0.7 }}>
            {loading || !prices[selected] ? (
              <Spinner size="s" />
            ) : (
              <>
                ≈ {prices[selected].ton.approx_formatted} · {prices[selected].usdt_ton.approx_formatted}
              </>
            )}
          </div>
        </>
      )}
    </List>
  );
}

function Radio({ checked }: { checked: boolean }) {
  return (
    <div
      style={{
        width: 22,
        height: 22,
        borderRadius: '50%',
        border: checked ? 'none' : '1.5px solid var(--tgui--hint_color)',
        background: checked ? 'var(--tgui--link_color)' : 'transparent',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {checked && (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
          <path d="M5 13L10 18L20 6" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
    </div>
  );
}

/** Степпер количества Stars: крупная цифра между «−» и «+». */
function Stepper({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const step = value >= 1000 ? 500 : value >= 200 ? 100 : 50;
  const clamp = (v: number) => Math.max(50, Math.min(100_000, v));

  return (
    <Section>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 22,
          padding: '22px 16px',
        }}
      >
        <RoundButton
          label="−"
          onClick={() => {
            haptic('tap');
            onChange(clamp(value - step));
          }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 150, justifyContent: 'center' }}>
          <StarIcon size={26} />
          <span style={{ fontSize: 38, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>{value}</span>
        </div>
        <RoundButton
          label="+"
          primary
          onClick={() => {
            haptic('tap');
            onChange(clamp(value + step));
          }}
        />
      </div>
    </Section>
  );
}

function RoundButton({
  label,
  onClick,
  primary,
}: {
  label: string;
  onClick: () => void;
  primary?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        width: 44,
        height: 44,
        borderRadius: '50%',
        border: 'none',
        cursor: 'pointer',
        fontSize: 22,
        fontWeight: 500,
        background: primary ? 'var(--tgui--link_color)' : 'var(--tgui--secondary_bg_color)',
        color: primary ? '#fff' : 'var(--tgui--text_color)',
      }}
    >
      {label}
    </button>
  );
}
