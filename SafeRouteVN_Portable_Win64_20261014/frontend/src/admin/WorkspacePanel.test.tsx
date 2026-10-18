import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { WorkspacePanel } from './WorkspacePanel';

it('collapses independently, removes hidden controls from interaction, and preserves drafts on restore', () => {
  render(<>
    <WorkspacePanel area="left"><input aria-label="Draft order" defaultValue="draft" /></WorkspacePanel>
    <WorkspacePanel area="right"><button>Selected plan</button></WorkspacePanel>
    <WorkspacePanel area="comparison"><p>Comparison metrics</p></WorkspacePanel>
  </>);
  const input = screen.getByLabelText('Draft order');
  fireEvent.change(input, { target: { value: 'edited draft' } });
  for (const name of ['bảng bên trái', 'bảng bên phải', 'Comparison']) {
    const button = screen.getByRole('button', { name: `Thu gọn ${name}` });
    const content = document.getElementById(button.getAttribute('aria-controls')!)!;
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(content).toHaveAttribute('inert');
    expect(content).toHaveAttribute('aria-hidden', 'true');
    expect(button.parentElement).toHaveClass('is-collapsed');
    fireEvent.click(screen.getByRole('button', { name: `Mở rộng ${name}` }));
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(content).not.toHaveAttribute('inert');
  }
  expect(screen.getByLabelText('Draft order')).toBe(input);
  expect(input).toHaveValue('edited draft');
});
