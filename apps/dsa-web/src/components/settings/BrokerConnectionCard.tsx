import { useMemo, useState } from 'react';
import type React from 'react';
import { Select } from '../common';
import type { ConfigValidationIssue, SystemConfigItem } from '../../types/systemConfig';
import { SettingsField } from './SettingsField';
import { SettingsSectionCard } from './SettingsSectionCard';

const BROKER_OPTIONS = [
  { value: '', label: '请选择券商' },
  { value: 'ibkr', label: 'Interactive Brokers（Flex Web Service）' },
];

type UserBrokerChoice = 'auto' | '' | 'ibkr';

export interface BrokerConnectionCardProps {
  items: SystemConfigItem[];
  disabled?: boolean;
  onChange: (key: string, value: string) => void;
  issueByKey: Record<string, ConfigValidationIssue[]>;
}

export const BrokerConnectionCard: React.FC<BrokerConnectionCardProps> = ({
  items,
  disabled = false,
  onChange,
  issueByKey,
}) => {
  const [userChoice, setUserChoice] = useState<UserBrokerChoice>('auto');

  const hasSavedCredentials = useMemo(
    () => items.some((i) => String(i.value ?? '').trim().length > 0),
    [items],
  );

  const resolvedBroker = useMemo(() => {
    if (userChoice === 'auto') {
      return hasSavedCredentials ? 'ibkr' : '';
    }
    return userChoice;
  }, [userChoice, hasSavedCredentials]);

  const handleBrokerChange = (value: string) => {
    setUserChoice(value === '' ? '' : 'ibkr');
  };

  return (
    <SettingsSectionCard
      title="券商账号连接"
      description="先在下方选择券商；选择 IBKR 后，再填写 Flex Token 与 Query ID（Client Portal，非 TWS）。保存配置后，组合页可使用「从 IBKR Flex 拉取持仓」。若已保存过凭据，进入页面时会自动选中 IBKR。"
    >
      <div className="space-y-4">
        <div className="space-y-2">
          <label className="text-xs font-medium text-secondary-text" htmlFor="broker-connection-provider">
            券商
          </label>
          <Select
            id="broker-connection-provider"
            value={resolvedBroker}
            onChange={handleBrokerChange}
            options={BROKER_OPTIONS}
            placeholder=""
            disabled={disabled}
          />
        </div>
        {resolvedBroker === 'ibkr' ? (
          <div className="space-y-5 pt-1">
            {items.length === 0 ? (
              <p className="text-xs text-muted-text">正在加载 IBKR 配置项…</p>
            ) : (
              items.map((item) => (
                <SettingsField
                  key={item.key}
                  item={item}
                  value={item.value}
                  disabled={disabled}
                  onChange={onChange}
                  issues={issueByKey[item.key] || []}
                />
              ))
            )}
          </div>
        ) : (
          <p className="text-xs leading-5 text-muted-text">请选择「Interactive Brokers」后，将显示 Token 与 Query ID 配置项。</p>
        )}
      </div>
    </SettingsSectionCard>
  );
};
