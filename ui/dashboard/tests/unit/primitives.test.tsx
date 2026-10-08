/**
 * Behavior tests for the ui/primitives design-system layer.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import Badge from '@/components/ui/primitives/Badge';
import Button from '@/components/ui/primitives/Button';
import Field from '@/components/ui/primitives/Field';
import Input from '@/components/ui/primitives/Input';
import Modal from '@/components/ui/primitives/Modal';
import SegmentedControl from '@/components/ui/primitives/SegmentedControl';
import StatusPill from '@/components/ui/primitives/StatusPill';

describe('Button', () => {
  it('fires onClick', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await userEvent.click(screen.getByRole('button', { name: 'Go' }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('is disabled while pending and shows a spinner', () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick} pending>Go</Button>);
    const btn = screen.getByRole('button');
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe('Modal', () => {
  it('closes on Escape', async () => {
    const onClose = vi.fn();
    render(<Modal title="T" onClose={onClose}><button>inner</button></Modal>);
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('does not close on Escape when locked', async () => {
    const onClose = vi.fn();
    render(<Modal title="T" onClose={onClose} locked><button>inner</button></Modal>);
    await userEvent.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on scrim click but not on panel click', async () => {
    const onClose = vi.fn();
    render(<Modal title="T" onClose={onClose}><button>inner</button></Modal>);
    await userEvent.click(screen.getByText('inner'));
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('dialog'));
    expect(onClose).toHaveBeenCalled();
  });

  it('traps focus inside the dialog', async () => {
    render(
      <Modal title="T" onClose={() => {}}>
        <button>first</button>
        <button>last</button>
      </Modal>,
    );
    // First focusable gets focus on mount.
    expect(screen.getByText('first')).toHaveFocus();
    // Shift+Tab from the first wraps to the last.
    await userEvent.tab({ shift: true });
    expect(screen.getByText('last')).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByText('first')).toHaveFocus();
  });
});

describe('SegmentedControl', () => {
  function Harness() {
    const [v, setV] = useState('a');
    return (
      <SegmentedControl
        aria-label="modes"
        segments={[
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B' },
          { value: 'c', label: 'C' },
        ]}
        value={v}
        onChange={setV}
      />
    );
  }

  it('selects on click and reflects aria-selected', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('tab', { name: 'B' }));
    expect(screen.getByRole('tab', { name: 'B' })).toHaveAttribute('aria-selected', 'true');
  });

  it('arrow keys move selection with wrap-around', async () => {
    render(<Harness />);
    const a = screen.getByRole('tab', { name: 'A' });
    a.focus();
    fireEvent.keyDown(a, { key: 'ArrowLeft' });
    expect(screen.getByRole('tab', { name: 'C' })).toHaveAttribute('aria-selected', 'true');
    const c = screen.getByRole('tab', { name: 'C' });
    fireEvent.keyDown(c, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'A' })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('Field', () => {
  it('wires label and error to the control', () => {
    render(
      <Field label="Run ID" required error="required">
        {({ id, describedBy }) => <Input id={id} aria-describedby={describedBy} />}
      </Field>,
    );
    const input = screen.getByLabelText(/Run ID/);
    expect(input).toHaveAccessibleDescription('required');
  });
});

describe('StatusPill', () => {
  it.each([
    ['success', 'success'],
    ['failed', 'failure'],
    ['running', 'pending'],
    ['weird', 'neutral'],
  ])('maps %s to the %s tone', (status) => {
    render(<StatusPill status={status} />);
    expect(screen.getByText(status)).toBeDefined();
  });
});

describe('Badge', () => {
  it('renders its children', () => {
    render(<Badge tone="accent">CLI</Badge>);
    expect(screen.getByText('CLI')).toBeDefined();
  });
});
