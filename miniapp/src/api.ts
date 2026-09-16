/**
 * Клиент к нашему бэкенду.
 *
 * Каждый запрос несёт initData в заголовке Authorization — по нему сервер
 * и определяет, кто мы. Никаких user_id в теле: их можно подделать, подпись —
 * нет.
 */

import { initData } from './telegram';

export type Asset = 'ton' | 'usdt_ton';
export type Product = 'premium' | 'stars';

export interface PriceEntry {
  base: string;
  approx: string;
  approx_formatted: string;
  label: string;
}

export interface Order {
  id: string;
  status: 'pending' | 'paid' | 'fulfilling' | 'fulfilled' | 'failed' | 'expired';
  title: string;
  product: Product;
  months: number | null;
  stars: number | null;
  recipient: string;
  asset: Asset;
  asset_label: string;
  amount: string;
  amount_units: number | null;
  amount_formatted: string;
  pay_to_address: string | null;
  expires_at: string | null;
  fragment_tx_id: string | null;
}

export interface TxRequest {
  valid_until: number;
  messages: Array<{ address: string; amount: string; payload?: string }>;
}

/**
 * Ошибка, текст которой уже пригоден для показа клиенту.
 * order — если сервер вместе с отказом вернул заказ, к которому надо перейти
 * (например, по прошлому заказу уже пришла оплата).
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly order?: Order,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `tma ${initData()}`,
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError('Нет связи с сервером. Проверь интернет и попробуй ещё раз.', 0);
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Пустое или не-JSON тело — ниже разберёмся по статусу.
  }

  if (!response.ok) {
    const payload = body as { error?: string; order?: Order } | null;
    const message = payload?.error ?? 'Что-то пошло не так, попробуй ещё раз.';
    throw new ApiError(message, response.status, payload?.order);
  }
  return body as T;
}

export const api = {
  price(params: { product: Product; months?: number; stars?: number }) {
    return request<{ prices: Record<Asset, PriceEntry> }>('/api/price', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  },

  createOrder(params: {
    product: Product;
    months?: number;
    stars?: number;
    asset: Asset;
    recipient: string;
  }) {
    return request<{ order: Order; reused: boolean }>('/api/orders', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  },

  /** Транзакция под конкретный подключённый кошелёк — её собирает сервер. */
  orderTx(orderId: string, walletAddress: string) {
    return request<TxRequest>(`/api/orders/${orderId}/tx`, {
      method: 'POST',
      body: JSON.stringify({ wallet_address: walletAddress }),
    });
  },

  order(orderId: string) {
    return request<{ order: Order }>(`/api/orders/${orderId}`);
  },

  activeOrder() {
    return request<{ order: Order | null }>('/api/orders/active');
  },
};
