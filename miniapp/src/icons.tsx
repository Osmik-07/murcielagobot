/**
 * Иконки — инлайновый SVG, не эмодзи: масштабируются и перекрашиваются.
 *
 * Акценты ровно два, оба «родные» для Telegram и потому узнаваемые:
 * градиент Telegram Premium и золото Telegram Stars. Больше нигде цвет
 * не выдумываем — остальное берётся из темы клиента.
 */

export const PREMIUM_GRADIENT =
  'linear-gradient(135deg, #6C93FF 0%, #976FFF 35%, #DF69D1 68%, #FFA85C 100%)';

export const STAR_GOLD = '#ffb300';

export function PremiumIcon({ size = 32 }: { size?: number }) {
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.28,
        background: PREMIUM_GRADIENT,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      <svg width={size * 0.56} height={size * 0.56} viewBox="0 0 24 24" fill="none">
        <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z" fill="#fff" />
      </svg>
    </div>
  );
}

export function StarIcon({ size = 20, boxed = false }: { size?: number; boxed?: boolean }) {
  const star = (
    <svg width={boxed ? size * 0.6 : size} height={boxed ? size * 0.6 : size} viewBox="0 0 24 24" fill="none">
      <path
        d="M12 2L14.9 8.6L22 9.3L16.7 14.1L18.2 21L12 17.4L5.8 21L7.3 14.1L2 9.3L9.1 8.6L12 2Z"
        fill={boxed ? '#fff' : STAR_GOLD}
      />
    </svg>
  );

  if (!boxed) return star;
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.28,
        background: STAR_GOLD,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      {star}
    </div>
  );
}

export function CheckIcon({ size = 40, color = '#fff' }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path d="M6 12.5L10.2 17L19 7" stroke={color} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Символ TON в фирменном круге — индикатор валюты, как в кошельках. */
export function TonLogo({ size = 40 }: { size?: number }) {
  return (
    <svg className="asset-logo" width={size} height={size} viewBox="0 0 56 56" fill="none">
      <circle cx="28" cy="28" r="28" fill="#0098EA" />
      <path
        d="M37.56 15.63H18.44c-3.52 0-5.74 3.79-3.98 6.86l11.8 20.45c.77 1.34 2.7 1.34 3.47 0l11.8-20.45c1.77-3.06-.46-6.86-3.97-6.86Zm-11.3 21.18-2.57-4.97-6.2-11.1c-.41-.7.1-1.62.95-1.62h7.82v17.69Zm12.25-16.07-6.2 11.1-2.57 4.97V19.12h7.82c.86 0 1.36.91.95 1.62Z"
        fill="#fff"
      />
    </svg>
  );
}

/** Знак Tether (USDT) — упрощённый, узнаваемый в размере иконки. */
export function UsdtLogo({ size = 40 }: { size?: number }) {
  return (
    <svg className="asset-logo" width={size} height={size} viewBox="0 0 56 56" fill="none">
      <circle cx="28" cy="28" r="28" fill="#26A17B" />
      <rect x="15" y="15" width="26" height="6" rx="1.5" fill="#fff" />
      <rect x="24.5" y="15" width="7" height="27" rx="1.5" fill="#fff" />
      <ellipse cx="28" cy="27.5" rx="12.5" ry="3.8" stroke="#fff" strokeWidth="2.4" />
    </svg>
  );
}

export function WalletIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <rect x="2.5" y="5.5" width="19" height="14" rx="3" stroke="currentColor" strokeWidth="1.8" />
      <path d="M2.5 10H21.5" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="17" cy="15" r="1.4" fill="currentColor" />
    </svg>
  );
}
