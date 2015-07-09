#
# Copyright 2015 Quantopian, Inc.
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

from abc import ABCMeta
from itertools import chain
from numbers import Integral
import numpy as np
import operator
import sqlite3
import warnings

from functools32 import lru_cache
from logbook import Logger
import pandas as pd
from pandas.tseries.tools import normalize_date
from six import with_metaclass, string_types

from zipline.errors import (
    ConsumeAssetMetaDataError,
    InvalidAssetType,
    MultipleSymbolsFound,
    RootSymbolNotFound,
    SidAssignmentError,
    SidNotFound,
    SymbolNotFound,
    MapAssetIdentifierIndexError,
)
from zipline.assets._assets import (
    Asset, Equity, Future
)

log = Logger('assets.py')

# Expected fields for an Asset's metadata
ASSET_FIELDS = [
    'sid',
    'asset_type',
    'symbol',
    'root_symbol',
    'asset_name',
    'start_date',
    'end_date',
    'first_traded',
    'exchange',
    'notice_date',
    'expiration_date',
    'contract_multiplier',
    # The following fields are for compatibility with other systems
    'file_name',  # Used as symbol
    'company_name',  # Used as asset_name
    'start_date_nano',  # Used as start_date
    'end_date_nano',  # Used as end_date
]


# Expected fields for an Asset's metadata
ASSET_TABLE_FIELDS = [
    'sid',
    'symbol',
    'asset_name',
    'start_date_nano',
    'end_date_nano',
    'first_traded_nano',
    'exchange',
]


# Expected fields for an Asset's metadata
FUTURE_TABLE_FIELDS = ASSET_TABLE_FIELDS + [
    'root_symbol',
    'notice_date_nano',
    'expiration_date_nano',
    'contract_multiplier',
]

EQUITY_TABLE_FIELDS = ASSET_TABLE_FIELDS

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


class AssetFinder(object):

    def __init__(self, metadata=None, allow_sid_assignment=True):

        self.sym_cache = {}
        self.future_chains_cache = {}
        self.fuzzy_match = {}

        # This flag controls if the AssetFinder is allowed to generate its own
        # sids. If False, metadata that does not contain a sid will raise an
        # exception when building assets.
        self.allow_sid_assignment = allow_sid_assignment

        self.conn = sqlite3.connect(':memory:')

        c = self.conn.cursor()

        c.execute("""
        CREATE TABLE equities
        (sid integer,
        symbol text,
        asset_name text,
        start_date_nano integer,
        end_date_nano integer,
        first_traded_nano integer,
        exchange text
        )""")

        c.execute("""
        CREATE TABLE futures
        (sid integer,
        symbol text,
        asset_name text,
        start_date_nano integer,
        end_date_nano integer,
        first_traded_nano integer,
        exchange text,
        root_symbol text,
        notice_date_nano integer,
        expiration_date_nano integer,
        contract_multiplier real
        )""")

        c.execute("""
        CREATE TABLE asset_router
        (sid integer,
        asset_type text)
        """)

        self.conn.commit()

        # The AssetFinder also holds a nested-dict of all metadata for
        # reference when building Assets
        self.metadata_cache = {}
        if metadata is not None:
            self.consume_metadata(metadata)

    @lru_cache(maxsize=None)
    def asset_type_by_sid(self, sid):
        c = self.conn.cursor()
        t = (sid,)
        c.row_factory = dict_factory
        query = 'select asset_type from asset_router where sid=?'
        c.execute(query, t)
        data = c.fetchone()
        if data is None:
            return
        return data['asset_type']

    def retrieve_asset(self, sid, default_none=False):
        if isinstance(sid, Asset):
            return sid

        asset_type = self.asset_type_by_sid(sid)
        if asset_type == 'equity':
            asset = self.equity_for_id(sid)
        elif asset_type == 'future':
            asset = self.futures_contract_for_id(sid)
        else:
            asset = None

        if asset is not None:
            return asset
        elif default_none:
            return None
        else:
            raise SidNotFound(sid=sid)

    @lru_cache(maxsize=None)
    def equity_for_id(self, sid):
        c = self.conn.cursor()
        t = (sid,)
        c.row_factory = dict_factory
        query = 'select {0} from equities where sid=?'.\
                format(", ".join(EQUITY_TABLE_FIELDS))
        c.execute(query, t)
        data = c.fetchone()
        if data:
            if data['start_date_nano']:
                data['start_date'] = pd.Timestamp(
                    data['start_date_nano'], tz='UTC')
            del data['start_date_nano']
            if data['end_date_nano']:
                data['end_date'] = pd.Timestamp(
                    data['end_date_nano'], tz='UTC')
            del data['end_date_nano']
            return Equity(**data)

    @lru_cache(maxsize=None)
    def futures_contract_for_id(self, contract_id):
        c = self.conn.cursor()
        t = (contract_id,)
        c.row_factory = dict_factory
        query = 'select {0} from futures where sid=?'.format(
            ", ".join(FUTURE_TABLE_FIELDS))
        c.execute(query, t)
        data = c.fetchone()
        if data:
            if data['start_date_nano']:
                data['start_date'] = pd.Timestamp(
                    data['start_date_nano'], tz='UTC')
            del data['start_date_nano']
            if data['end_date_nano']:
                data['end_date'] = pd.Timestamp(
                    data['end_date_nano'], tz='UTC')
            del data['end_date_nano']
            if data['notice_date_nano']:
                data['notice_date'] = pd.Timestamp(
                    data['notice_date_nano'], tz='UTC')
            del data['notice_date_nano']
            if data['first_traded_nano']:
                data['first_traded'] = pd.Timestamp(
                    data['first_traded_nano'], tz='UTC')
            del data['first_traded_nano']
            if data['expiration_date_nano']:
                data['expiration_date'] = pd.Timestamp(
                    data['expiration_date_nano'], tz='UTC')
            del data['expiration_date_nano']
            return Future(**data)

    @staticmethod
    def _lookup_symbol_in_infos(infos, as_of_date):
        """
        Search a list of symbols matching a given asset for the most recent
        known symbol as of as_of_date.

        Returns a pair of (Asset, bool), representing the best match we
        found for as_of_date, and whether or not that match was actually
        trading at as_of_date.

        If no entry in infos started before as_of_date, return (None, False).
        """
        # Sort entries by end_date before iterating.  If asset start and end
        # dates were always disjoint, then we could sort by either start or
        # end_date and get the same sorting.
        infos = sorted(infos, key=operator.attrgetter('end_date'))

        # Find the newest asset that started before as_of_date.
        candidates = [i for i in infos
                      if (i.start_date is None or i.start_date <= as_of_date)
                      and (i.end_date is None or as_of_date <= i.end_date)]

        # If one SID exists for symbol, return that symbol
        if len(candidates) == 1:
            return candidates[0], True

        # If no SID exists for symbol, return SID with the
        # highest-but-not-over end_date
        if len(candidates) == 0:
            candidates = [i for i in infos
                          if i.end_date < as_of_date]
            return (candidates[-1], False) if candidates else (None, False)

        # If multiple SIDs exist for symbol, return latest start_date with
        # end_date as a tie-breaker
        if len(candidates) > 1:
            best_candidate = sorted(
                candidates,
                key=lambda x: (x.start_date, x.end_date)
            )[-1]
            return best_candidate, True

    def lookup_symbol_resolve_multiple(self, symbol, as_of_date=None):
        """
        Return matching Asset of name symbol in database.

        If multiple Assets are found and as_of_date is not set,
        raises MultipleSymbolsFound.

        If no Asset was active at as_of_date, and allow_expired is False
        raises SymbolNotFound.
        """
        if as_of_date is not None:
            as_of_date = normalize_date(as_of_date)

        c = self.conn.cursor()
        c.row_factory = dict_factory

        if as_of_date:
            # If one SID exists for symbol, return that symbol
            t = (symbol, as_of_date.value, as_of_date.value)
            query = ("select sid from equities "
                     "where symbol=? " +
                     "and start_date_nano<=? " +
                     "and end_date_nano>=?")
            c.execute(query, t)
            candidates = c.fetchall()

            if len(candidates) == 1:
                return self.equity_for_id(candidates[0]['sid'])

            # If no SID exists for symbol, return SID with the
            # highest-but-not-over end_date
            if len(candidates) == 0:
                t = (symbol, as_of_date.value)
                query = ("select sid from equities "
                         "where symbol=? " +
                         "and start_date_nano<=? " +
                         "order by end_date_nano desc " +
                         "limit 1")
                c.execute(query, t)
                data = c.fetchone()

                if data:
                    return self.equity_for_id(data['sid'])

            if len(candidates) > 1:
                t = (symbol, as_of_date.value)
                query = ("select sid from equities "
                         "where symbol=? " +
                         "and start_date_nano<=? " +
                         "order by start_date_nano desc, end_date_nano desc" +
                         "limit 1")
                c.execute(query, t)
                data = c.fetchone()

                if data:
                    return self.equity_for_id(data['sid'])

            raise SymbolNotFound(symbol=symbol)

        else:
            t = (symbol,)
            query = ("select sid from equities where symbol=?")
            c.execute(query, t)
            data = c.fetchone()

            if len(data) == 1:
                return self.equity_for_id(data['sid'])
            elif not data:
                raise SymbolNotFound(symbol=symbol)
            else:
                raise MultipleSymbolsFound(symbol=symbol,
                                           options=str(data))

    def lookup_symbol(self, symbol, as_of_date, fuzzy=None):
        """
        If a fuzzy string is provided, then we try various symbols based on
        the provided symbol.  This is to facilitate mapping from a broker's
        symbol to ours in cases where mapping to the broker's symbol loses
        information. For example, if we have CMCS_A, but a broker has CMCSA,
        when the broker provides CMCSA, it can also provide fuzzy='_',
        so we can find a match by inserting an underscore.
        """
        symbol = symbol.upper()
        as_of_date = normalize_date(as_of_date)

        if not fuzzy:
            try:
                return self.lookup_symbol_resolve_multiple(symbol, as_of_date)
            except SymbolNotFound:
                return None
        else:
            try:
                return self.fuzzy_match[(symbol, fuzzy, as_of_date)]
            except KeyError:
                # if symbol is CMCSA and fuzzy is '_', then
                # try CMCSA, then CMCS_A, then CMC_SA, etc.
                for fuzzy_symbol in chain(
                        (symbol,),
                        (symbol[:i] + fuzzy + symbol[i:]
                         for i in range(len(symbol) - 1, 0, -1))):

                    infos = self.sym_cache.get(fuzzy_symbol)
                    if infos:
                        info, date_match = self._lookup_symbol_in_infos(
                            infos,
                            as_of_date,
                        )

                        if info is not None and date_match:
                            self.fuzzy_match[(symbol, fuzzy, as_of_date)] = \
                                info
                            return info
                else:
                    self.fuzzy_match[(symbol, fuzzy, as_of_date)] = None

    def _sort_future_chains(self):
        """ Sort by increasing expiration date the list of contracts
        for each root symbol in the future cache.
        """
        notice_key = operator.attrgetter('notice_date')

        for root_symbol in self.future_chains_cache:
            self.future_chains_cache[root_symbol].sort(key=notice_key)

    def lookup_future_chain(self, root_symbol, as_of_date, knowledge_date):
        """ Return the futures chain for a given root symbol.

        Parameters
        ----------
        root_symbol : str
            Root symbol of the desired future.
        as_of_date : pd.Timestamp
            Date at which the chain determination is rooted. I.e. the
            existing contract that expires first after (or on) this date is
            the primary contract, etc.
        knowledge_date : pd.Timestamp
            Date for determining which contracts exist for inclusion in
            this chain. Contracts exist only if they have a start_date
            on or before this date.

        Returns
        -------
        [Future]
        """
        c = self.conn.cursor()
        t = {'root_symbol': root_symbol,
             'as_of_date': as_of_date.value,
             'knowledge_date': knowledge_date.value}
        c.execute("""
        select sid from futures
        where root_symbol=:root_symbol
        and expiration_date_nano >= :as_of_date
        and start_date_nano <= :knowledge_date
        order by expiration_date_nano asc
        """, t)
        sids = [r[0] for r in c.fetchall()]
        return [self.futures_contract_for_id(sid) for sid in sids]

    @property
    def sids(self):
        return self.cache.keys()

    @property
    def assets(self):
        return self.cache.values()

    def _lookup_generic_scalar(self,
                               asset_convertible,
                               as_of_date,
                               matches,
                               missing):
        """
        Convert asset_convertible to an asset.

        On success, append to matches.
        On failure, append to missing.
        """
        try:
            if isinstance(asset_convertible, Asset):
                matches.append(asset_convertible)

            elif isinstance(asset_convertible, Integral):
                result = self.retrieve_asset(int(asset_convertible))
                if result is None:
                    raise SymbolNotFound(symbol=asset_convertible)
                matches.append(result)

            elif isinstance(asset_convertible, string_types):
                # Throws SymbolNotFound on failure to match.
                matches.append(
                    self.lookup_symbol_resolve_multiple(
                        asset_convertible,
                        as_of_date,
                    )
                )
            else:
                raise NotAssetConvertible(
                    "Input was %s, not AssetConvertible."
                    % asset_convertible
                )

        except SymbolNotFound:
            missing.append(asset_convertible)
            return None

    def lookup_generic(self,
                       asset_convertible_or_iterable,
                       as_of_date):
        """
        Convert a AssetConvertible or iterable of AssetConvertibles into
        a list of Asset objects.

        This method exists primarily as a convenience for implementing
        user-facing APIs that can handle multiple kinds of input.  It should
        not be used for internal code where we already know the expected types
        of our inputs.

        Returns a pair of objects, the first of which is the result of the
        conversion, and the second of which is a list containing any values
        that couldn't be resolved.
        """
        matches = []
        missing = []

        # Interpret input as scalar.
        if isinstance(asset_convertible_or_iterable, AssetConvertible):
            self._lookup_generic_scalar(
                asset_convertible=asset_convertible_or_iterable,
                as_of_date=as_of_date,
                matches=matches,
                missing=missing,
            )
            try:
                return matches[0], missing
            except IndexError:
                if hasattr(asset_convertible_or_iterable, '__int__'):
                    raise SidNotFound(sid=asset_convertible_or_iterable)
                else:
                    raise SymbolNotFound(symbol=asset_convertible_or_iterable)

        # Interpret input as iterable.
        try:
            iterator = iter(asset_convertible_or_iterable)
        except TypeError:
            raise NotAssetConvertible(
                "Input was not a AssetConvertible "
                "or iterable of AssetConvertible."
            )

        for obj in iterator:
            self._lookup_generic_scalar(obj, as_of_date, matches, missing)
        return matches, missing

    def map_identifier_index_to_sids(self, index, as_of_date):
        """
        This method is for use in sanitizing a user's DataFrame or Panel
        inputs.

        Takes the given index of identifiers, checks their types, builds assets
        if necessary, and returns a list of the sids that correspond to the
        input index.

        Parameters
        __________
        index : Iterable
            An iterable containing ints, strings, or Assets
        as_of_date : pandas.Timestamp
            A date to be used to resolve any dual-mapped symbols

        Returns
        _______
        List
            A list of integer sids corresponding to the input index
        """
        # This method assumes that the type of the objects in the index is
        # consistent and can, therefore, be taken from the first identifier
        first_identifier = index[0]

        # Ensure that input is AssetConvertible (integer, string, or Asset)
        if not isinstance(first_identifier, AssetConvertible):
            raise MapAssetIdentifierIndexError(obj=first_identifier)

        # If sids are provided, no mapping is necessary
        if isinstance(first_identifier, Integral):
            return index

        # If symbols or Assets are provided, construction and mapping is
        # necessary
        self.consume_identifiers(index)
        self.populate_cache()

        # Look up all Assets for mapping
        matches = []
        missing = []
        for identifier in index:
            self._lookup_generic_scalar(identifier, as_of_date,
                                        matches, missing)

        # Handle missing assets
        if len(missing) > 0:
            warnings.warn("Missing assets for identifiers: " + missing)

        # Return a list of the sids of the found assets
        return [asset.sid for asset in matches]

    def insert_metadata(self, identifier, **kwargs):
        """
        Inserts the given metadata kwargs to the entry for the given
        identifier. Matching fields in the existing entry will be overwritten.
        :param identifier: The identifier for which to insert metadata
        :param kwargs: The keyed metadata to insert
        """
        entry = self.metadata_cache.get(identifier, {})

        for key, value in kwargs.items():
            # Do not accept invalid fields
            if key not in ASSET_FIELDS:
                continue
            # Do not accept Nones
            if value is None:
                continue
            # Do not accept empty strings
            if value == '':
                continue
            # Do not accept nans from dataframes
            if isinstance(value, float) and np.isnan(value):
                continue
            entry[key] = value

        # Check if the sid is declared
        try:
            entry['sid']
        except KeyError:
            # If the identifier is not a sid, assign one
            if hasattr(identifier, '__int__'):
                entry['sid'] = identifier.__int__()
            else:
                if self.allow_sid_assignment:
                    # Assign the sid the value of its insertion order.
                    # This assumes that we are assigning values to all assets.
                    entry['sid'] = len(self.metadata_cache)
                else:
                    raise SidAssignmentError(identifier=identifier)


                # If the file_name is in the kwargs, it will be used as the symbol
        try:
            entry['symbol'] = entry.pop('file_name')
        except KeyError:
            pass

        # If the identifier coming in was a string and there is no defined
        # symbol yet, set the symbol to the incoming identifier
        try:
            entry['symbol']
            pass
        except KeyError:
            if isinstance(identifier, string_types):
                entry['symbol'] = identifier

        # If the company_name is in the kwargs, it may be the asset_name
        try:
            company_name = entry.pop('company_name')
            try:
                entry['asset_name']
            except KeyError:
                entry['asset_name'] = company_name
        except KeyError:
            pass

        # If dates are given as nanos, pop them
        try:
            entry['start_date'] = entry.pop('start_date_nano')
        except KeyError:
            pass
        try:
            entry['end_date'] = entry.pop('end_date_nano')
        except KeyError:
            pass
        try:
            entry['notice_date'] = entry.pop('notice_date_nano')
        except KeyError:
            pass
        try:
            entry['expiration_date'] = entry.pop('expiration_date_nano')
        except KeyError:
            pass

        # Process dates to Timestamps
        try:
            entry['start_date'] = pd.Timestamp(entry['start_date'], tz='UTC')
        except KeyError:
            pass
        try:
            entry['end_date'] = pd.Timestamp(entry['end_date'], tz='UTC')
        except KeyError:
            pass
        try:
            entry['notice_date'] = pd.Timestamp(entry['notice_date'],
                                                tz='UTC')
        except KeyError:
            pass
        try:
            entry['expiration_date'] = pd.Timestamp(entry['expiration_date'],
                                                    tz='UTC')
        except KeyError:
            pass

        # Build an Asset of the appropriate type, default to Equity
        asset_type = entry.pop('asset_type', 'equity')
        if asset_type.lower() == 'equity':
            asset = Equity(**entry)
            c = self.conn.cursor()
            t = (asset.sid,
                 asset.symbol,
                 asset.asset_name,
                 asset.start_date.value if asset.start_date else None,
                 asset.end_date.value if asset.end_date else None,
                 asset.first_traded.value if asset.first_traded else None,
                 asset.exchange)
            c.execute("""INSERT INTO equities(
            sid,
            symbol,
            asset_name,
            start_date_nano,
            end_date_nano,
            first_traded_nano,
            exchange)
            VALUES(?, ?, ?, ?, ?, ?, ?)""", t)

            t = (asset.sid,
                 'equity')
            c.execute("""INSERT INTO asset_router(sid, asset_type)
            VALUES(?, ?)""", t)
            self.conn.commit()

        elif asset_type.lower() == 'future':
            asset = Future(**entry)
            c = self.conn.cursor()
            t = (asset.sid,
                 asset.symbol,
                 asset.asset_name,
                 asset.start_date.value if asset.start_date else None,
                 asset.end_date.value if asset.end_date else None,
                 asset.first_traded.value if asset.first_traded else None,
                 asset.exchange,
                 asset.root_symbol,
                 asset.notice_date.value if asset.notice_date else None,
                 asset.expiration_date.value
                 if asset.expiration_date else None,
                 asset.contract_multiplier)
            c.execute("""INSERT INTO futures(
            sid,
            symbol,
            asset_name,
            start_date_nano,
            end_date_nano,
            first_traded_nano,
            exchange,
            root_symbol,
            notice_date_nano,
            expiration_date_nano,
            contract_multiplier)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", t)
            self.conn.commit()

            t = (asset.sid,
                 'future')
            c.execute("""INSERT INTO asset_router(sid, asset_type)
            VALUES(?, ?)""", t)
            self.conn.commit()
        else:
            raise InvalidAssetType(asset_type=asset_type)

        self.metadata_cache[identifier] = entry

    def consume_identifiers(self, identifiers):
        """
        Consumes the given identifiers in to the metadata cache of this
        AssetFinder.
        """
        for identifier in identifiers:
            # Handle case where full Assets are passed in
            # For example, in the creation of a DataFrameSource, the source's
            # 'sid' args may be full Assets
            if isinstance(identifier, Asset):
                sid = identifier.sid
                metadata = identifier.to_dict()
                metadata['asset_type'] = identifier.__class__.__name__
                self.insert_metadata(identifier=sid, **metadata)
            else:
                self.insert_metadata(identifier)

    def consume_metadata(self, metadata):
        """
        Consumes the provided metadata in to the metadata cache. The
        existing values in the cache will be overwritten when there
        is a conflict.
        :param metadata: The metadata to be consumed
        """
        # Handle dicts
        if isinstance(metadata, dict):
            self._insert_metadata_dict(metadata)
        # Handle DataFrames
        elif isinstance(metadata, pd.DataFrame):
            self._insert_metadata_dataframe(metadata)
        # Handle readables
        elif hasattr(metadata, 'read'):
            self._insert_metadata_readable(metadata)
        else:
            raise ConsumeAssetMetaDataError(obj=metadata)

    def clear_metadata(self):
        self.metadata_cache = {}

    def _insert_metadata_dataframe(self, dataframe):
        for identifier, row in dataframe.iterrows():
            self.insert_metadata(identifier, **row)

    def _insert_metadata_dict(self, dict):
        for identifier, entry in dict.items():
            self.insert_metadata(identifier, **entry)

    def _insert_metadata_readable(self, readable):
        for row in readable.read():
            # Parse out the row of the readable object
            metadata_dict = {}
            for field in ASSET_FIELDS:
                try:
                    row_value = row[field]
                    # Avoid passing placeholders
                    if row_value and (row_value is not 'None'):
                        metadata_dict[field] = row[field]
                except KeyError:
                    continue
                except IndexError:
                    continue
            # Locate the identifier, fail if not found
            if 'sid' in metadata_dict:
                identifier = metadata_dict['sid']
            elif 'symbol' in metadata_dict:
                identifier = metadata_dict['symbol']
            else:
                raise ConsumeAssetMetaDataError(obj=row)
            self.insert_metadata(identifier, **metadata_dict)


class AssetConvertible(with_metaclass(ABCMeta)):
    """
    ABC for types that are convertible to integer-representations of
    Assets.

    Includes Asset, six.string_types, and Integral
    """
    pass

AssetConvertible.register(Integral)
AssetConvertible.register(Asset)
# Use six.string_types for Python2/3 compatibility
for type in string_types:
    AssetConvertible.register(type)


class NotAssetConvertible(ValueError):
    pass
