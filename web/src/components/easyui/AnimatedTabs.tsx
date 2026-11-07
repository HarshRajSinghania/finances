/*
 * From EasyUI by Suraj Maurya (https://easyui.site, github.com/Surajmaurya1/easyui),
 * MIT licensed — see ./LICENSE. Adapted: imports, and colours mapped to this
 * app's design tokens so it follows the light/dark theme.
 */
'use client';

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { cn } from "@/lib/utils";
import { motionTransitions } from "./lib/motion-tokens";

export interface TabItem {
  id: string;
  label: string;
  icon?: React.ReactNode;
  content?: React.ReactNode;
  badge?: string | number;
}

export interface AnimatedTabsProps {
  tabs: TabItem[];
  defaultTab?: string;
  activeTab?: string;
  onChange?: (tabId: string) => void;
  className?: string;
  tabsContainerClassName?: string;
  renderContent?: boolean;
  layoutId?: string;
  variant?: 'default' | 'glass' | 'pill';
}

export const AnimatedTabs: React.FC<AnimatedTabsProps> = ({
  tabs,
  defaultTab,
  activeTab: controlledActiveTab,
  onChange,
  className,
  tabsContainerClassName,
  renderContent = true,
  layoutId: customLayoutId,
  variant = 'default',
}) => {
  const [internalActiveTab, setInternalActiveTab] = useState<string>(defaultTab || tabs[0]?.id || '');
  const activeTab = controlledActiveTab !== undefined ? controlledActiveTab : internalActiveTab;
  const uniqueId = React.useId();
  const indicatorLayoutId = customLayoutId || `active-tab-indicator-${uniqueId}`;

  React.useEffect(() => {
    if (defaultTab && controlledActiveTab === undefined) {
      setInternalActiveTab(defaultTab);
    }
  }, [defaultTab, controlledActiveTab]);

  const handleTabClick = (tabId: string) => {
    if (controlledActiveTab === undefined) {
      setInternalActiveTab(tabId);
    }
    onChange?.(tabId);
  };

  const currentTabObj = tabs.find((t) => t.id === activeTab);
  const isGlass = variant === 'glass' || variant === 'pill';

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      <div
        className={cn(
          'flex items-center gap-1 self-start select-none',
          isGlass
            ? 'p-1 rounded-full bg-[var(--color-surface)] border border-[var(--color-hairline)]'
            : 'p-1 rounded-lg bg-[var(--color-surface)] border border-[var(--color-hairline)]',
          tabsContainerClassName
        )}
      >
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              aria-pressed={isActive}
              onClick={() => handleTabClick(tab.id)}
              className={cn(
                'relative text-xs sm:text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-series-1)]/60 select-none flex items-center gap-2 cursor-pointer',
                isGlass
                  ? 'px-4 py-1.5 rounded-full'
                  : 'px-3.5 py-1.5 rounded-md',
                isActive
                  ? 'text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-muted)] hover:text-[var(--color-ink-secondary)]'
              )}
            >
              {isActive && (
                <motion.div
                  layoutId={indicatorLayoutId}
                  className={cn(
                    'absolute inset-0 shadow-xs',
                    isGlass
                      ? 'rounded-full bg-[var(--color-raised)] border border-[var(--color-hairline)]'
                      : 'rounded-md bg-[var(--color-raised)] border border-[var(--color-hairline)]'
                  )}
                  transition={motionTransitions.springMorph}
                />
              )}
              <span className="relative z-10 flex items-center gap-1.5">
                {tab.icon && <span>{tab.icon}</span>}
                {tab.label}
                {tab.badge && (
                  <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-[var(--color-plane)] border border-[var(--color-hairline)] text-[var(--color-ink-secondary)]">
                    {tab.badge}
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>

      {renderContent && (
        <div className="relative min-h-[60px]">
          <AnimatePresence mode="wait">
            {currentTabObj?.content && (
              <motion.div
                key={activeTab}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={motionTransitions.springGentle}
                className="text-sm text-[var(--color-ink-secondary)]"
              >
                {currentTabObj.content}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
};
