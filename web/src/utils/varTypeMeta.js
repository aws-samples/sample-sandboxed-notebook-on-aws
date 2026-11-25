/**
 * varTypeMeta — shared variable-type presentation helpers so the Variables SIDE
 * PANEL and the per-cell "Variables" chips render identical icons, colors, and
 * grouping. Previously each place had its own partial copy of the icon map, which
 * drifted (the cell chips used cryptic `#.`/`◇` glyphs while the panel grouped
 * cleanly into Data / Collections / Scalars). Keep this the single source of truth.
 */

// Glyph per Python type. Matches the side panel exactly.
export const TYPE_ICONS = {
  DataFrame: '📊', Series: '📈', ndarray: '🔢', list: '[ ]', dict: '{ }',
  tuple: '( )', str: 'abc', int: '#', float: '#.', bool: '⊘', NoneType: '∅',
}

// NumPy scalar type names (np.int64 → "int64", etc.). The backend normalizes these
// to the Python name on newer VM images, but older VMs still report the numpy name,
// so we also fold them in HERE as a fallback — otherwise a numpy scalar wrongly
// lands in the "Other" group with a "◇" icon. Keyed by their canonical Python kind.
const NUMPY_INT = ['int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64', 'intp', 'uintp']
const NUMPY_FLOAT = ['float16', 'float32', 'float64', 'longdouble', 'float128']
const NUMPY_BOOL = ['bool_']
const NUMPY_COMPLEX = ['complex64', 'complex128']

// Canonical Python type for a (possibly numpy) type name — so downstream icon/
// color/kind logic only has to reason about Python types.
function canonicalType(type) {
  if (NUMPY_INT.includes(type)) return 'int'
  if (NUMPY_FLOAT.includes(type)) return 'float'
  if (NUMPY_BOOL.includes(type)) return 'bool'
  if (NUMPY_COMPLEX.includes(type)) return 'complex'
  if (type === 'str_') return 'str'
  return type
}

export function getTypeIcon(type) {
  return TYPE_ICONS[canonicalType(type)] || '◇'
}

// CSS class for the type-colored icon (see .var-type-* in Sidebar.css).
export function getTypeColor(rawType) {
  const type = canonicalType(rawType)
  if (['DataFrame', 'Series'].includes(type)) return 'var-type-dataframe'
  if (['list', 'tuple', 'set'].includes(type)) return 'var-type-collection'
  if (['dict'].includes(type)) return 'var-type-dict'
  if (['int', 'float', 'complex'].includes(type)) return 'var-type-number'
  if (['str'].includes(type)) return 'var-type-string'
  if (['bool', 'NoneType'].includes(type)) return 'var-type-bool'
  return 'var-type-other'
}

// Coarse grouping for the group-by-kind view, and the order those groups render in.
export const KIND_ORDER = ['Data', 'Collections', 'Scalars', 'Other']

export function kindOf(rawType) {
  const type = canonicalType(rawType)
  if (['DataFrame', 'Series', 'ndarray'].includes(type)) return 'Data'
  if (['list', 'tuple', 'dict', 'set', 'frozenset'].includes(type)) return 'Collections'
  if (['int', 'float', 'complex', 'bool', 'str', 'NoneType'].includes(type)) return 'Scalars'
  return 'Other'
}
