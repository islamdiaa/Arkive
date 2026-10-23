import type { Config } from 'tailwindcss';

// Theme colours are CSS variables, which Tailwind can't split into channels,
// so opacity modifiers (bg-danger/10, border-warning/30) generated nothing.
// color-mix() applies the modifier to any colour; without one, <alpha-value>
// is 1 and the colour is unchanged.
type ColorTree = { [key: string]: string | ColorTree };

function withAlpha(colors: ColorTree): ColorTree {
  return Object.fromEntries(
    Object.entries(colors).map(([name, value]) => [
      name,
      typeof value === 'string'
        ? `color-mix(in srgb, ${value} calc(<alpha-value> * 100%), transparent)`
        : withAlpha(value),
    ]),
  );
}

export default {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: ['./src/**/*.{html,js,svelte,ts}'],
  theme: {
    extend: {
      colors: withAlpha({
        // Legacy flat tokens (backward compat)
        page: 'var(--page-bg)',
        surface: 'var(--surface)',
        'surface-hover': 'var(--surface-hover)',
        'surface-subtle': 'var(--surface-subtle)',
        'text-secondary': 'var(--text-secondary)',

        // Background scale
        bg: {
          base: 'var(--bg-base)',
          app: 'var(--bg-app)',
          sidebar: 'var(--bg-sidebar)',
          surface: 'var(--bg-surface)',
          'surface-hover': 'var(--bg-surface-hover)',
          input: 'var(--bg-input)',
          elevated: 'var(--bg-elevated)',
          overlay: 'var(--bg-overlay)',
        },

        // Border scale
        border: {
          DEFAULT: 'var(--border-default)',
          muted: 'var(--border-muted)',
          strong: 'var(--border-strong)',
          focus: 'var(--border-focus)',
        },

        // Text scale
        text: {
          DEFAULT: 'var(--text)',
          primary: 'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          tertiary: 'var(--text-tertiary)',
          muted: 'var(--text-muted)',
          disabled: 'var(--text-disabled)',
          link: 'var(--text-link)',
          'on-primary': 'var(--text-on-primary)',
        },

        // Accent colors (5-stop scales)
        primary: {
          DEFAULT: 'var(--color-primary)',
          hover: 'var(--color-primary-hover)',
          strong: 'var(--color-primary-strong)',
          muted: 'var(--color-primary-muted)',
          bg: 'var(--color-primary-bg)',
        },
        success: {
          DEFAULT: 'var(--color-success)',
          hover: 'var(--color-success-hover)',
          strong: 'var(--color-success-strong)',
          muted: 'var(--color-success-muted)',
          bg: 'var(--color-success-bg)',
        },
        warning: {
          DEFAULT: 'var(--color-warning)',
          hover: 'var(--color-warning-hover)',
          strong: 'var(--color-warning-strong)',
          muted: 'var(--color-warning-muted)',
          bg: 'var(--color-warning-bg)',
        },
        danger: {
          DEFAULT: 'var(--color-danger)',
          hover: 'var(--color-danger-hover)',
          strong: 'var(--color-danger-strong)',
          muted: 'var(--color-danger-muted)',
          bg: 'var(--color-danger-bg)',
        },
        info: {
          DEFAULT: 'var(--color-info)',
          hover: 'var(--color-info-hover)',
          strong: 'var(--color-info-strong)',
          muted: 'var(--color-info-muted)',
          bg: 'var(--color-info-bg)',
        },
        neutral: {
          DEFAULT: 'var(--color-neutral)',
          muted: 'var(--color-neutral-muted)',
          bg: 'var(--color-neutral-bg)',
        },

        // Semantic aliases
        accent: {
          DEFAULT: 'var(--color-accent)',
          hover: 'var(--color-accent-hover)',
        },
        error: {
          DEFAULT: 'var(--color-error)',
          bg: 'var(--color-error-bg)',
        },
        folder: 'var(--color-folder)',

        // Flat legacy aliases still used by components
        elevated: 'var(--bg-elevated)',
      }),
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'SF Mono', 'monospace'],
      },
      fontSize: {
        xs: ['0.75rem', { lineHeight: '1.33' }],
        sm: ['0.8125rem', { lineHeight: '1.38' }],
        base: ['0.875rem', { lineHeight: '1.5' }],
        lg: ['1rem', { lineHeight: '1.5' }],
        xl: ['1.25rem', { lineHeight: '1.4' }],
        '2xl': ['1.5rem', { lineHeight: '1.25' }],
        '3xl': ['2rem', { lineHeight: '1.2' }],
        'display': ['2.25rem', { lineHeight: '1.1', fontWeight: '700' }],
      },
      borderRadius: { sm: '4px', md: '6px', lg: '8px', xl: '12px' },
      boxShadow: {
        sm: '0 1px 2px rgba(0,0,0,0.3)',
        md: '0 2px 8px rgba(0,0,0,0.3)',
        lg: '0 4px 16px rgba(0,0,0,0.4)',
        xl: '0 8px 32px rgba(0,0,0,0.5)',
        focus: '0 0 0 3px rgba(56,139,253,0.3)',
        'focus-danger': '0 0 0 3px rgba(248,81,73,0.3)',
      },
      zIndex: {
        dropdown: '10',
        sticky: '20',
        'modal-backdrop': '30',
        modal: '40',
        toast: '50',
        tooltip: '60',
      },
    },
  },
  plugins: [],
} satisfies Config;
