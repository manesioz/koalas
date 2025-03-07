#
# Copyright (C) 2019 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Wrappers for Indexes to behave similar to pandas Index, MultiIndex.
"""

from functools import partial
from typing import Any, List, Optional, Tuple, Union

import pandas as pd
from pandas.api.types import is_list_like, is_interval_dtype, is_bool_dtype, \
    is_categorical_dtype, is_integer_dtype, is_float_dtype, is_numeric_dtype, is_object_dtype

from pyspark import sql as spark
from pyspark.sql import functions as F

from databricks import koalas as ks  # For running doctests and reference resolution in PyCharm.
from databricks.koalas.config import get_option
from databricks.koalas.exceptions import PandasNotImplementedError
from databricks.koalas.base import IndexOpsMixin
from databricks.koalas.frame import DataFrame
from databricks.koalas.internal import _InternalFrame
from databricks.koalas.missing.indexes import _MissingPandasLikeIndex, _MissingPandasLikeMultiIndex
from databricks.koalas.series import Series
from databricks.koalas.utils import name_like_string


class Index(IndexOpsMixin):
    """
    Koalas Index that corresponds to Pandas Index logically. This might hold Spark Column
    internally.

    :ivar _kdf: The parent dataframe
    :type _kdf: DataFrame
    :ivar _scol: Spark Column instance
    :type _scol: pyspark.Column

    Parameters
    ----------
    data : DataFrame or list
        Index can be created by DataFrame or list
    dtype : dtype, default None
        Data type to force. Only a single dtype is allowed. If None, infer
    name : name of index, hashable

    See Also
    --------
    MultiIndex : A multi-level, or hierarchical, Index.

    Examples
    --------
    >>> ks.DataFrame({'a': ['a', 'b', 'c']}, index=[1, 2, 3]).index
    Int64Index([1, 2, 3], dtype='int64')

    >>> ks.DataFrame({'a': [1, 2, 3]}, index=list('abc')).index
    Index(['a', 'b', 'c'], dtype='object')

    >>> Index([1, 2, 3])
    Int64Index([1, 2, 3], dtype='int64')

    >>> Index(list('abc'))
    Index(['a', 'b', 'c'], dtype='object')
    """

    def __init__(self, data: Union[DataFrame, list], dtype=None, name=None,
                 scol: Optional[spark.Column] = None) -> None:
        if isinstance(data, DataFrame):
            assert dtype is None
            assert name is None
            kdf = data
        else:
            assert scol is None
            kdf = DataFrame(index=pd.Index(data=data, dtype=dtype, name=name))
        if scol is None:
            scol = kdf._internal.index_scols[0]
        internal = kdf._internal.copy(scol=scol,
                                      column_index=kdf._internal.index_names,
                                      column_scols=kdf._internal.index_scols,
                                      column_index_names=None)
        IndexOpsMixin.__init__(self, internal, kdf)

    def _with_new_scol(self, scol: spark.Column) -> 'Index':
        """
        Copy Koalas Index with the new Spark Column.

        :param scol: the new Spark Column
        :return: the copied Index
        """
        return Index(self._kdf, scol=scol)

    @property
    def size(self) -> int:
        """
        Return an int representing the number of elements in this object.

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'],
        ...                   index=list('abcd'))
        >>> df.index.size
        4

        >>> df.set_index('dogs', append=True).index.size
        4
        """
        return len(self._kdf)  # type: ignore

    def to_pandas(self) -> pd.Index:
        """
        Return a pandas Index.

        .. note:: This method should only be used if the resulting Pandas object is expected
                  to be small, as all the data is loaded into the driver's memory.

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'],
        ...                   index=list('abcd'))
        >>> df['dogs'].index.to_pandas()
        Index(['a', 'b', 'c', 'd'], dtype='object')
        """
        sdf = self._kdf._sdf.select(self._scol)
        internal = self._kdf._internal.copy(
            sdf=sdf,
            index_map=[(sdf.schema[0].name, self._kdf._internal.index_names[0])],
            column_index=[], column_scols=[], column_index_names=None)
        return DataFrame(internal)._to_internal_pandas().index

    toPandas = to_pandas

    @property
    def spark_type(self):
        """ Returns the data type as defined by Spark, as a Spark DataType object."""
        return self.to_series().spark_type

    @property
    def has_duplicates(self) -> bool:
        """
        If index has duplicates, return True, otherwise False.

        Examples
        --------
        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=list('aac'))
        >>> kdf.index.has_duplicates
        True

        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=[list('abc'), list('def')])
        >>> kdf.index.has_duplicates
        False

        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=[list('aac'), list('eef')])
        >>> kdf.index.has_duplicates
        True
        """
        df = self._kdf._sdf.select(self._scol)
        col = df.columns[0]

        return df.select(F.count(col) != F.countDistinct(col)).first()[0]

    @property
    def name(self) -> Union[str, Tuple[str, ...]]:
        """Return name of the Index."""
        return self.names[0]

    @name.setter
    def name(self, name: Union[str, Tuple[str, ...]]) -> None:
        self.names = [name]

    @property
    def names(self) -> List[Union[str, Tuple[str, ...]]]:
        """Return names of the Index."""
        return [name if name is None or len(name) > 1 else name[0]
                for name in self._kdf._internal.index_names]

    @names.setter
    def names(self, names: List[Union[str, Tuple[str, ...]]]) -> None:
        if not is_list_like(names):
            raise ValueError('Names must be a list-like')
        internal = self._kdf._internal
        if len(internal.index_map) != len(names):
            raise ValueError('Length of new names must be {}, got {}'
                             .format(len(internal.index_map), len(names)))

        names = [name if isinstance(name, (tuple, type(None))) else (name,) for name in names]
        self._kdf._internal = internal.copy(index_map=list(zip(internal.index_columns, names)))

    @property
    def nlevels(self) -> int:
        """
        Number of levels in Index & MultiIndex.

        Examples
        --------
        >>> kdf = ks.DataFrame({"a": [1, 2, 3]}, index=pd.Index(['a', 'b', 'c'], name="idx"))
        >>> kdf.index.nlevels
        1

        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=[list('abc'), list('def')])
        >>> kdf.index.nlevels
        2
        """
        return len(self._kdf._internal.index_columns)

    def rename(self, name: Union[str, Tuple[str, ...]], inplace: bool = False):
        """
        Alter Index name.
        Able to set new names without level. Defaults to returning new index.

        Parameters
        ----------
        name : label or list of labels
            Name(s) to set.
        inplace : boolean, default False
            Modifies the object directly, instead of creating a new Index.

        Returns
        -------
        Index
            The same type as the caller or None if inplace is True.

        Examples
        --------
        >>> df = ks.DataFrame({'a': ['A', 'C'], 'b': ['A', 'B']}, columns=['a', 'b'])
        >>> df.index.rename("c")
        Int64Index([0, 1], dtype='int64', name='c')

        >>> df.set_index("a", inplace=True)
        >>> df.index.rename("d")
        Index(['A', 'C'], dtype='object', name='d')

        You can also change the index name in place.

        >>> df.index.rename("e", inplace=True)
        Index(['A', 'C'], dtype='object', name='e')

        >>> df  # doctest: +NORMALIZE_WHITESPACE
           b
        e
        A  A
        C  B
        """
        index_columns = self._kdf._internal.index_columns
        assert len(index_columns) == 1

        if isinstance(name, str):
            name = (name,)
        internal = self._kdf._internal.copy(index_map=[(index_columns[0], name)])

        if inplace:
            self._kdf._internal = internal
            return self
        else:
            return Index(DataFrame(internal), scol=self._scol)

    def to_series(self, name: Union[str, Tuple[str, ...]] = None) -> Series:
        """
        Create a Series with both index and values equal to the index keys
        useful with map for returning an indexer based on an index.

        Parameters
        ----------
        name : string, optional
            name of resulting Series. If None, defaults to name of original
            index

        Returns
        -------
        Series : dtype will be based on the type of the Index values.

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'],
        ...                   index=list('abcd'))
        >>> df['dogs'].index.to_series()
        a    a
        b    b
        c    c
        d    d
        Name: 0, dtype: object
        """
        kdf = self._kdf
        scol = self._scol
        if name is not None:
            scol = scol.alias(name_like_string(name))
        column_index = [None] if len(kdf._internal.index_map) > 1 else kdf._internal.index_names
        return Series(kdf._internal.copy(scol=scol,
                                         column_index=column_index,
                                         column_index_names=None),
                      anchor=kdf)

    def is_boolean(self):
        """
        Return if the current index type is a boolean type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[True]).index.is_boolean()
        True
        """
        return is_bool_dtype(self.dtype)

    def is_categorical(self):
        """
        Return if the current index type is a categorical type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[1]).index.is_categorical()
        False
        """
        return is_categorical_dtype(self.dtype)

    def is_floating(self):
        """
        Return if the current index type is a floating type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[1]).index.is_floating()
        False
        """
        return is_float_dtype(self.dtype)

    def is_integer(self):
        """
        Return if the current index type is a integer type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[1]).index.is_integer()
        True
        """
        return is_integer_dtype(self.dtype)

    def is_interval(self):
        """
        Return if the current index type is an interval type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[1]).index.is_interval()
        False
        """
        return is_interval_dtype(self.dtype)

    def is_numeric(self):
        """
        Return if the current index type is a numeric type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=[1]).index.is_numeric()
        True
        """
        return is_numeric_dtype(self.dtype)

    def is_object(self):
        """
        Return if the current index type is a object type.

        Examples
        --------
        >>> ks.DataFrame({'a': [1]}, index=["a"]).index.is_object()
        True
        """
        return is_object_dtype(self.dtype)

    def unique(self, level=None):
        """
        Return unique values in the index.
        Be aware the order of unique values might be different than pandas.Index.unique

        :param level: int or str, optional, default is None
        :return: Index without deuplicates

        Examples
        --------
        >>> ks.DataFrame({'a': ['a', 'b', 'c']}, index=[1, 1, 3]).index.unique()
        Int64Index([1, 3], dtype='int64')

        >>> ks.DataFrame({'a': ['a', 'b', 'c']}, index=['d', 'e', 'e']).index.unique()
        Index(['e', 'd'], dtype='object')
        """
        if level is not None:
            self._validate_index_level(level)
        sdf = self._kdf._sdf.select(self._scol.alias(self._internal.index_columns[0])).distinct()
        return DataFrame(_InternalFrame(sdf=sdf, index_map=self._kdf._internal.index_map)).index

    def _validate_index_level(self, level):
        """
        Validate index level.
        For single-level Index getting level number is a no-op, but some
        verification must be done like in MultiIndex.
        """
        if isinstance(level, int):
            if level < 0 and level != -1:
                raise IndexError(
                    "Too many levels: Index has only 1 level,"
                    " %d is not a valid level number" % (level,)
                )
            elif level > 0:
                raise IndexError(
                    "Too many levels:" " Index has only 1 level, not %d" % (level + 1)
                )
        elif level != self.name:
            raise KeyError(
                "Requested level ({}) does not match index name ({})".format(
                    level, self.name
                )
            )

    def copy(self, name=None):
        """
        Make a copy of this object. name sets those attributes on the new object.

        Parameters
        ----------
        name : string, optional
            to set name of index

        Examples
        --------
        >>> df = ks.DataFrame([[1, 2], [4, 5], [7, 8]],
        ...                   index=['cobra', 'viper', 'sidewinder'],
        ...                   columns=['max_speed', 'shield'])
        >>> df
                    max_speed  shield
        cobra               1       2
        viper               4       5
        sidewinder          7       8
        >>> df.index
        Index(['cobra', 'viper', 'sidewinder'], dtype='object')

        Copy index

        >>> df.index.copy()
        Index(['cobra', 'viper', 'sidewinder'], dtype='object')

        Copy index with name

        >>> df.index.copy(name='snake')
        Index(['cobra', 'viper', 'sidewinder'], dtype='object', name='snake')
        """
        internal = self._kdf._internal.copy()
        result = Index(ks.DataFrame(internal), scol=self._scol)
        if name:
            result.name = name
        return result

    def __getattr__(self, item: str) -> Any:
        if hasattr(_MissingPandasLikeIndex, item):
            property_or_func = getattr(_MissingPandasLikeIndex, item)
            if isinstance(property_or_func, property):
                return property_or_func.fget(self)  # type: ignore
            else:
                return partial(property_or_func, self)
        raise AttributeError("'Index' object has no attribute '{}'".format(item))

    def __repr__(self):
        max_display_count = get_option("display.max_rows")
        if max_display_count is None:
            return repr(self.to_pandas())

        pindex = self._kdf.head(max_display_count + 1).index._with_new_scol(self._scol).to_pandas()

        pindex_length = len(pindex)
        repr_string = repr(pindex[:max_display_count])

        if pindex_length > max_display_count:
            footer = '\nShowing only the first {}'.format(max_display_count)
            return repr_string + footer
        return repr_string

    def __iter__(self):
        return _MissingPandasLikeIndex.__iter__(self)


class MultiIndex(Index):
    """
    Koalas MultiIndex that corresponds to Pandas MultiIndex logically. This might hold Spark Column
    internally.

    :ivar _kdf: The parent dataframe
    :type _kdf: DataFrame
    :ivar _scol: Spark Column instance
    :type _scol: pyspark.Column

    See Also
    --------
    Index : A single-level Index.

    Examples
    --------
    >>> ks.DataFrame({'a': ['a', 'b', 'c']}, index=[[1, 2, 3], [4, 5, 6]]).index  # doctest: +SKIP
    MultiIndex([(1, 4),
                (2, 5),
                (3, 6)],
               )

    >>> ks.DataFrame({'a': [1, 2, 3]}, index=[list('abc'), list('def')]).index  # doctest: +SKIP
    MultiIndex([('a', 'd'),
                ('b', 'e'),
                ('c', 'f')],
               )
    """

    def __init__(self, kdf: DataFrame):
        assert len(kdf._internal._index_map) > 1
        scol = F.struct(kdf._internal.index_scols)
        data_columns = kdf._sdf.select(scol).columns
        internal = kdf._internal.copy(scol=scol,
                                      column_index=[(col, None) for col in data_columns],
                                      column_index_names=None)
        IndexOpsMixin.__init__(self, internal, kdf)

    def any(self, *args, **kwargs):
        raise TypeError("cannot perform any with this index type: MultiIndex")

    def all(self, *args, **kwargs):
        raise TypeError("cannot perform all with this index type: MultiIndex")

    @staticmethod
    def from_tuples(tuples, sortorder=None, names=None):
        """
        Convert list of tuples to MultiIndex.

        Parameters
        ----------
        tuples : list / sequence of tuple-likes
            Each tuple is the index of one row/column.
        sortorder : int or None
            Level of sortedness (must be lexicographically sorted by that level).
        names : list / sequence of str, optional
            Names for the levels in the index.

        Returns
        -------
        index : MultiIndex

        Examples
        --------

        >>> tuples = [(1, 'red'), (1, 'blue'),
        ...           (2, 'red'), (2, 'blue')]
        >>> ks.MultiIndex.from_tuples(tuples, names=('number', 'color'))  # doctest: +SKIP
        MultiIndex([(1,  'red'),
                    (1, 'blue'),
                    (2,  'red'),
                    (2, 'blue')],
                   names=['number', 'color'])
        """
        return DataFrame(index=pd.MultiIndex.from_tuples(
            tuples=tuples, sortorder=sortorder, names=names)).index

    @staticmethod
    def from_arrays(arrays, sortorder=None, names=None):
        """
        Convert arrays to MultiIndex.

        Parameters
        ----------
        arrays: list / sequence of array-likes
            Each array-like gives one level’s value for each data point. len(arrays)
            is the number of levels.
        sortorder: int or None
            Level of sortedness (must be lexicographically sorted by that level).
        names: list / sequence of str, optional
            Names for the levels in the index.

        Returns
        -------
        index: MultiIndex

        Examples
        --------

        >>> arrays = [[1, 1, 2, 2], ['red', 'blue', 'red', 'blue']]
        >>> ks.MultiIndex.from_arrays(arrays, names=('number', 'color'))  # doctest: +SKIP
        MultiIndex([(1,  'red'),
                    (1, 'blue'),
                    (2,  'red'),
                    (2, 'blue')],
                   names=['number', 'color'])
        """
        return DataFrame(index=pd.MultiIndex.from_arrays(
            arrays=arrays, sortorder=sortorder, names=names
        )).index

    @property
    def name(self) -> str:
        raise PandasNotImplementedError(class_name='pd.MultiIndex', property_name='name')

    @name.setter
    def name(self, name: str) -> None:
        raise PandasNotImplementedError(class_name='pd.MultiIndex', property_name='name')

    def to_pandas(self) -> pd.MultiIndex:
        """
        Return a pandas MultiIndex.

        .. note:: This method should only be used if the resulting Pandas object is expected
                  to be small, as all the data is loaded into the driver's memory.

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'],
        ...                   index=[list('abcd'), list('efgh')])
        >>> df['dogs'].index.to_pandas()  # doctest: +SKIP
        MultiIndex([('a', 'e'),
                    ('b', 'f'),
                    ('c', 'g'),
                    ('d', 'h')],
                   )
        """
        # TODO: We might need to handle internal state change.
        # So far, we don't have any functions to change the internal state of MultiIndex except for
        # series-like operations. In that case, it creates new Index object instead of MultiIndex.
        return self._kdf[[]]._to_internal_pandas().index

    toPandas = to_pandas

    def unique(self, level=None):
        raise PandasNotImplementedError(class_name='MultiIndex', method_name='unique')

    # TODO: add 'name' parameter after pd.MultiIndex.name is implemented
    def copy(self):
        """
        Make a copy of this object.
        """
        internal = self._kdf._internal.copy()
        result = MultiIndex(ks.DataFrame(internal))
        return result

    def __getattr__(self, item: str) -> Any:
        if hasattr(_MissingPandasLikeMultiIndex, item):
            property_or_func = getattr(_MissingPandasLikeMultiIndex, item)
            if isinstance(property_or_func, property):
                return property_or_func.fget(self)  # type: ignore
            else:
                return partial(property_or_func, self)
        raise AttributeError("'MultiIndex' object has no attribute '{}'".format(item))

    def rename(self, name, inplace=False):
        raise NotImplementedError()

    @property
    def levels(self) -> list:
        """
        Names of index columns in list.

        .. note:: Be aware of the possibility of running into out
            of memory issue if returned list is huge.

        Examples
        --------
        >>> mi = pd.MultiIndex.from_arrays((list('abc'), list('def')))
        >>> mi.names = ['level_1', 'level_2']
        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=mi)
        >>> kdf.index.levels
        [['a', 'b', 'c'], ['d', 'e', 'f']]

        >>> mi = pd.MultiIndex.from_arrays((list('bac'), list('fee')))
        >>> mi.names = ['level_1', 'level_2']
        >>> kdf = ks.DataFrame({'a': [1, 2, 3]}, index=mi)
        >>> kdf.index.levels
        [['a', 'b', 'c'], ['e', 'f']]
        """
        scols = self._kdf._internal.index_scols
        row = self._kdf._sdf.select([F.collect_set(scol) for scol in scols]).first()

        # use sorting is because pandas doesn't care the appearance order of level
        # names, so e.g. if ['b', 'd', 'a'] will return as ['a', 'b', 'd']
        return [sorted(col) for col in row]

    def __repr__(self):
        max_display_count = get_option("display.max_rows")
        if max_display_count is None:
            return repr(self.to_pandas())

        pindex = self._kdf.head(max_display_count + 1).index.to_pandas()

        pindex_length = len(pindex)
        repr_string = repr(pindex[:max_display_count])

        if pindex_length > max_display_count:
            footer = '\nShowing only the first {}'.format(max_display_count)
            return repr_string + footer
        return repr_string

    def __iter__(self):
        return _MissingPandasLikeMultiIndex.__iter__(self)
