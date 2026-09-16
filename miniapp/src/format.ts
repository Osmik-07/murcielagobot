/**
 * Как показывать деньги.
 *
 * Цены на экранах выбора — ориентир: к ним на сервере добавляется случайный
 * «хвост» в минимальных единицах, чтобы платёж можно было опознать по сумме.
 * Показывать такой ориентир с девятью знаками бессмысленно, поэтому — два
 * знака, и округление ВВЕРХ: клиент не должен увидеть сумму меньше той,
 * что потом появится в кошельке. Точную сумму он увидит при подписании.
 */

export function approxAmount(raw: string, asset: 'ton' | 'usdt_ton'): string {
  const value = Number(raw);
  if (!Number.isFinite(value)) return '—';
  const rounded = Math.ceil(value * 100) / 100;
  const label = asset === 'ton' ? 'TON' : 'USDT';
  return `${rounded.toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${label}`;
}

/**
 * Точная сумма (например, уже оплаченная): без округления — это факт, а не
 * ориентир, «≈» здесь было бы неправдой. Лишние нули в конце убираем.
 */
export function exactAmount(raw: string, asset: 'ton' | 'usdt_ton'): string {
  const [whole, fraction = ''] = raw.split('.');
  const trimmed = fraction.replace(/0+$/, '').padEnd(2, '0');
  const label = asset === 'ton' ? 'TON' : 'USDT';
  return `${Number(whole).toLocaleString('ru-RU')},${trimmed} ${label}`;
}

export function months(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return `${n} месяц`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${n} месяца`;
  return `${n} месяцев`;
}

export function starsCount(n: number): string {
  return n.toLocaleString('ru-RU');
}

/**
 * Реальная выгода длинного срока относительно помесячной цены трёхмесячного.
 *
 * Считается из живых цен Fragment, а не зашивается: цифра на бейдже — это
 * обещание клиенту, и оно должно совпадать с тем, что он заплатит.
 * Меньше 5% не показываем — это шум, а не выгода.
 */
export function discountPercent(
  priceForPeriod: string,
  periodMonths: number,
  baselinePrice: string,
  baselineMonths: number,
): number | null {
  const perMonth = Number(priceForPeriod) / periodMonths;
  const baselinePerMonth = Number(baselinePrice) / baselineMonths;
  if (!Number.isFinite(perMonth) || !Number.isFinite(baselinePerMonth) || baselinePerMonth <= 0) {
    return null;
  }
  const percent = Math.round((1 - perMonth / baselinePerMonth) * 100);
  return percent >= 5 ? percent : null;
}
