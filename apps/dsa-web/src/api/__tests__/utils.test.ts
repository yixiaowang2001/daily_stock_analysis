import { describe, expect, it } from 'vitest';

import { toCamelCase } from '../utils';

describe('toCamelCase', () => {
  it('preserves stock symbol dictionary keys when converting API responses', () => {
    const result = toCamelCase<{
      symbolNames: Record<string, string>;
      items: Array<{ symbolNames: Record<string, string>; config: { symbolNames: Record<string, string> } }>;
    }>({
      symbol_names: {
        'BRK.B': 'Berkshire Hathaway Inc. Class B',
        NVDA: 'NVIDIA Corporation',
      },
      items: [
        {
          symbol_names: {
            AAPL: 'Apple Inc.',
          },
          config: {
            symbol_names: {
              GOOG: 'Alphabet Inc. Class C',
            },
          },
        },
      ],
    });

    expect(result.symbolNames.NVDA).toBe('NVIDIA Corporation');
    expect(result.symbolNames['BRK.B']).toBe('Berkshire Hathaway Inc. Class B');
    expect(result.symbolNames.nvda).toBeUndefined();
    expect(result.items[0].symbolNames.AAPL).toBe('Apple Inc.');
    expect(result.items[0].config.symbolNames.GOOG).toBe('Alphabet Inc. Class C');
  });
});
