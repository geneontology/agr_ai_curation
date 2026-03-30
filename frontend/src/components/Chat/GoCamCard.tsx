import { useState } from 'react'

interface GoCamCardProps {
  modelId: string
  noctuaBase?: string
}

/**
 * Renders a GO-CAM model card with links and an embedded pathway preview.
 *
 * Detects model IDs (gomodel:xxx) in chat messages and provides:
 * - Direct link to the Noctua graph editor
 * - Direct link to the pathway view
 * - Embedded pathway preview (toggleable)
 * - Note about saving the model
 */
export default function GoCamCard({
  modelId,
  noctuaBase = 'http://noctua-dev.berkeleybop.org'
}: GoCamCardProps) {
  const [showPreview, setShowPreview] = useState(false)

  const editorUrl = `${noctuaBase}/editor/graph/${modelId}`
  const pathwayUrl = `${noctuaBase}/workbench/noctua-alliance-pathway-preview/?model_id=${encodeURIComponent(modelId)}`

  return (
    <div style={{
      border: '1px solid #e0e0e0',
      borderRadius: '8px',
      padding: '12px 16px',
      margin: '8px 0',
      backgroundColor: '#f8fdf8',
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        marginBottom: '8px',
      }}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="#2e7d32">
          <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
        </svg>
        <span style={{ fontWeight: 600, color: '#2e7d32' }}>GO-CAM Model</span>
        <code style={{
          fontSize: '0.85em',
          backgroundColor: '#e8f5e9',
          padding: '2px 6px',
          borderRadius: '4px',
        }}>
          {modelId}
        </code>
      </div>

      <div style={{ display: 'flex', gap: '12px', marginBottom: '8px' }}>
        <a
          href={editorUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            color: '#1565c0',
            textDecoration: 'none',
            fontSize: '0.9em',
          }}
        >
          Open in Graph Editor
        </a>
        <a
          href={pathwayUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            color: '#1565c0',
            textDecoration: 'none',
            fontSize: '0.9em',
          }}
        >
          Open Pathway View
        </a>
        <button
          onClick={() => setShowPreview(!showPreview)}
          style={{
            background: 'none',
            border: 'none',
            color: '#1565c0',
            cursor: 'pointer',
            fontSize: '0.9em',
            padding: 0,
          }}
        >
          {showPreview ? 'Hide Preview' : 'Show Preview'}
        </button>
      </div>

      {showPreview && (
        <div style={{
          border: '1px solid #e0e0e0',
          borderRadius: '4px',
          overflow: 'hidden',
          marginBottom: '8px',
        }}>
          <iframe
            src={pathwayUrl}
            style={{
              width: '100%',
              height: '400px',
              border: 'none',
            }}
            title={`GO-CAM ${modelId}`}
          />
        </div>
      )}

      <div style={{
        fontSize: '0.8em',
        color: '#757575',
        fontStyle: 'italic',
      }}>
        Note: Model is unsaved until you save it in the Noctua editor.
        Unsaved models will not appear on the Noctua landing page.
      </div>
    </div>
  )
}
