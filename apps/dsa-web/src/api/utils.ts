import camelcaseKeys from 'camelcase-keys';

const DEFAULT_STOP_PATHS = [
    'symbol_names',
    'items.symbol_names',
    'config.symbol_names',
    'items.config.symbol_names',
    'symbol_names_source',
    'items.config.symbol_names_source',
    'config.symbol_names_source',
    'symbol_names_raw_candidates',
    'items.config.symbol_names_raw_candidates',
    'config.symbol_names_raw_candidates',
];

/**
 * 将 snake_case 对象键转换为 camelCase
 * @param data API 响应数据 (snake_case)
 * @returns 转换后的 camelCase 对象
 */
export function toCamelCase<T>(data: unknown): T {
    if (data === null || data === undefined) {
        return data as T;
    }
    return camelcaseKeys(data as Record<string, unknown>, {
        deep: true,
        stopPaths: DEFAULT_STOP_PATHS,
    }) as T;
}
