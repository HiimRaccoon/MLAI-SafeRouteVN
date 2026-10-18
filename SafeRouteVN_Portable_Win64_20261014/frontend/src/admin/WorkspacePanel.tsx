import { useState, type ReactNode } from 'react';
import { PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from 'lucide-react';

/** Keep contents mounted so collapsing never resets selection, drafts or map state. */
export function WorkspacePanel({ area, children }: { area: 'left' | 'right' | 'comparison'; children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const name = area === 'left' ? 'bảng bên trái' : area === 'right' ? 'bảng bên phải' : 'Comparison';
  const label = `${collapsed ? 'Mở rộng' : 'Thu gọn'} ${name}`;
  const Icon = area === 'left' ? (collapsed ? PanelLeftOpen : PanelLeftClose) : (collapsed ? PanelRightOpen : PanelRightClose);
  return <div className={`workspace-panel workspace-panel-${area}${collapsed ? ' is-collapsed' : ''}`}>
    <button type="button" className="workspace-toggle" aria-label={label} title={label}
      aria-expanded={!collapsed} aria-controls={`workspace-${area}-content`} onClick={() => setCollapsed(value => !value)}>
      {area === 'comparison' ? <span className="workspace-toggle-bar" aria-hidden="true" /> : <Icon size={22} aria-hidden="true" />}
    </button>
    <div id={`workspace-${area}-content`} className="workspace-panel-content" inert={collapsed} aria-hidden={collapsed || undefined}>
      {children}
    </div>
  </div>;
}
