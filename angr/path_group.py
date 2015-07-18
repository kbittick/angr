import ana
import simuvex
import mulpyplexer

import logging
l = logging.getLogger('angr.path_group')

class PathGroup(ana.Storable):
    '''
    Path groups are the future.

    Path groups allow you to wrangle multiple paths in a slick way.  Paths are
    organized into "stashes", which you can step forward, filter, merge, and
    move around as you wish. This allows you to, for example, step two
    different stashes of paths at different rates, then merge them together.

    Note that path groups are immutable by default (all operations will return
    new PathGroup objects). See the immutable argument to __init__.

    Stashes can be accessed as attributes (i.e., pg.active). A mulpyplexed
    stash can be retrieved by prepending the name with "mp_" (i.e.,
    pg.mp_active).
    '''

    def __init__(self, project, active_paths=None, stashes=None, hierarchy=None, veritesting=None,
                 veritesting_options=None, immutable=None, resilience=None, save_unconstrained=None,
                 save_unsat=None):
        '''
        Initializes a new PathGroup.

        @param project: an angr.Project instance
        @param active_paths: active paths to seed the "active" stash with.
        @param stashes: a dictionary to use as the stash store
        @param hierarchy: a PathHierarchy object to use to track path reachability
        @param immutable: if True (the default), all operations will return a new
                          PathGroup. Otherwise, all operations will modify the
                          PathGroup (and return it, for consistency and chaining).
        '''
        self._project = project
        self._hierarchy = PathHierarchy() if hierarchy is None else hierarchy
        self._immutable = True if immutable is None else immutable
        self._veritesting = False if veritesting is None else veritesting
        self._resilience = False if resilience is None else resilience
        self._veritesting_options = { } if veritesting_options is None else veritesting_options

        # public options
        self.save_unconstrained = False if save_unconstrained is None else save_unconstrained
        self.save_unsat = False if save_unsat is None else save_unsat

        self.stashes = {
            'active': [ ] if active_paths is None else active_paths,
            'stashed': [ ],
            'pruned': [ ],
            'unsat': [ ],
            'errored': [ ],
            'deadended': [ ],
            'unconstrained': [ ]
        } if stashes is None else stashes

    #
    # Util functions
    #

    def copy(self):
        return PathGroup(self._project, stashes=self._copy_stashes(immutable=True), hierarchy=self._hierarchy, immutable=self._immutable)

    def _copy_stashes(self, immutable=None):
        '''
        Returns a copy of the stashes (if immutable) or the stashes themselves
        (if not immutable). Used to abstract away immutability.
        '''
        if self._immutable if immutable is None else immutable:
            return { k:list(v) for k,v in self.stashes.items() }
        else:
            return self.stashes

    def _copy_paths(self, paths):
        '''
        Returns a copy of a list of paths (if immutable) or the paths themselves
        (if not immutable). Used to abstract away immutability.
        '''
        if self._immutable:
            return [ p.copy() for p in paths ]
        else:
            return paths

    def _successor(self, new_stashes):
        '''
        Creates a new PathGroup with the provided stashes (if immutable), or sets
        the stashes (if not immutable). Used to abstract away immutability.

        @returns a PathGroup
        '''
        if '_DROP' in new_stashes:
            del new_stashes['_DROP']

        if not self._immutable:
            self.stashes = new_stashes
            return self
        else:
            return PathGroup(self._project, stashes=new_stashes, hierarchy=self._hierarchy, immutable=self._immutable)

    @staticmethod
    def _condition_to_lambda(condition, default=False):
        '''
        Translates an integer, set, or list into a lambda that checks a path address
        against the given addresses.

        @param condition: an integer, set, or list to convert to a lambda
        @param default: the default return value of the lambda (in case condition
                        is None). Default: false.
        @returns a lambda that takes a path and returns True or False
        '''
        if condition is None:
            condition = lambda p: default

        if isinstance(condition, (int, long)):
            condition = { condition }

        if isinstance(condition, (tuple, set, list)):
            addrs = set(condition)
            condition = lambda p: p.addr in addrs

        return condition

    @staticmethod
    def _filter_paths(filter_func, paths):
        '''
        Filters a sequence of paths according to a filter_func.

        @param filter_func: the filter function. Should take a path as input and
                            return a boolean
        @param paths: a sequence of paths

        @returns a tuple, with the first element the matching paths and the second
                 element the non-matching paths.
        '''
        l.debug("Filtering %d paths", len(paths))
        match = [ ]
        nomatch = [ ]

        for p in paths:
            if filter_func is None or filter_func(p):
                l.debug("... path %s matched!", p)
                match.append(p)
            else:
                l.debug("... path %s didn't match!", p)
                nomatch.append(p)

        l.debug("... returning %d matches and %d non-matches", len(match), len(nomatch))
        return match, nomatch

    def _one_step(self, stash=None, successor_func=None, check_func=None):
        '''
        Takes a single step in a given stash.

        @param stash: the name of the stash (default: 'active').
        @param successor_func: if provided, this function is called with the path as
                               its only argument. It should return the path's
                               successors. If this is None, path.successors is used,
                               instead.

        @param returns the successor PathGroup
        '''
        stash = 'active' if stash is None else stash

        new_stashes = self._copy_stashes()
        new_active = [ ]

        for a in self.stashes[stash]:
            try:
                has_stashed = False # Flag that whether we have put a into a stash or not
                successors = [ ]

                veritesting_worked = False
                if self._veritesting:
                    sse = self._project.analyses.SSE(a, **self._veritesting_options)
                    if sse.result['result'] and sse.result['final_path_group']:
                        pg = sse.result['final_path_group']
                        pg.stash(from_stash='deviated', to_stash='active')
                        pg.stash(from_stash='successful', to_stash='active')
                        successors = pg.active
                        pg.drop(stash='active')
                        for s in pg.stashes:
                            if s not in new_stashes:
                                new_stashes[s] = []
                            new_stashes[s] += pg.stashes[s]
                        veritesting_worked = True

                if not veritesting_worked:
                    # `check_func` will not be called for Veritesting, this is
                    # intended so that we can avoid unnecessarily creating
                    # Path._run

                    if (check_func is not None and check_func(a)) or (check_func is None and a.errored):
                        # This path has error(s)!
                        if isinstance(a.error, PathUnreachableError):
                            new_stashes['pruned'].append(a)
                        else:
                            self._hierarchy.unreachable(a)
                            new_stashes['errored'].append(a)
                        has_stashed = True
                    else:
                        if successor_func is not None:
                            successors = successor_func(a)
                        else:
                            successors = a.successors
                            if self.save_unconstrained:
                                if 'unconstrained' not in new_stashes:
                                    new_stashes['unconstrained'] = [ ]
                                new_stashes['unconstrained'] += a.unconstrained_successors
                            if self.save_unsat:
                                if 'unsat' not in new_stashes:
                                    new_stashes['unsat'] = [ ]
                                new_stashes['unsat'] += a.unsat_successors

                if not has_stashed:
                    if len(successors) == 0:
                        new_stashes['deadended'].append(a)
                    else:
                        new_active.extend(successors)
            except Exception: # this is JUST FOR THE CGC. After that, we'll downgrade it to Angr/Sim/ClaripyError
                if not self._resilience:
                    raise
                else:
                    l.warning("PathGroup resilience squashed an exception", exc_info=True)

        new_stashes[stash] = new_active
        return self._successor(new_stashes)

    @staticmethod
    def _move(stashes, filter_func, from_stash, to_stash):
        '''
        Moves all stashes that match the filter_func from from_stash to to_stash.

        @returns a new stashes dictionary
        '''
        to_move, to_keep = PathGroup._filter_paths(filter_func, stashes[from_stash])
        if to_stash not in stashes:
            stashes[to_stash] = [ ]

        stashes[to_stash].extend(to_move)
        stashes[from_stash] = to_keep
        return stashes

    def __repr__(self):
        s = "<PathGroup with "
        s += ', '.join(("%d %s" % (len(v),k)) for k,v in self.stashes.items() if len(v) != 0)
        s += ">"
        return s

    def __getattr__(self, k):
        if k.startswith('mp_'):
            return mulpyplexer.MP(self.stashes[k[3:]])
        else:
            return self.stashes[k]

    def __dir__(self):
        return sorted(set(self.__dict__.keys() + dir(super(PathGroup, self)) + self.stashes.keys() + [ 'mp_'+k for k in self.stashes.keys() ]))

    #
    # Interface
    #

    def apply(self, path_func=None, stash_func=None, stash=None):
        '''
        Applies a given function to a given stash.

        @param path_func: a function to apply to every path. Should take a path and
                          return a path. The returned path will take the place of the
                          old path. If the function *doesn't* return a path, the old
                          path will be used. If the function returns a list of paths,
                          they will replace the original paths.
        @param stash_func: a function to apply to the whole stash. Should take a
                           list of paths and return a list of paths. The resulting
                           list will replace the stash.

        If both path_func and stash_func are provided, path_func is applied first,
        then stash_func is applied on the results.

        @returns the resulting PathGroup
        '''
        stash = 'active' if stash is None else stash

        new_stashes = self._copy_stashes()
        new_paths = new_stashes[stash]
        if path_func is not None:
            new_new_paths = [ ]
            for p in new_paths:
                np = path_func(p)
                if isinstance(np, Path):
                    new_new_paths.append(np)
                elif isinstance(np, (list, tuple, set)):
                    new_new_paths.extend(np)
                else:
                    new_new_paths.append(p)
            new_paths = new_new_paths
        if stash_func is not None:
            new_paths = stash_func(new_paths)

        new_stashes[stash] = new_paths
        return self._successor(new_stashes)

    def split(self, stash_splitter=None, stash_ranker=None, path_ranker=None, limit=None, from_stash=None, to_stash=None):
        '''
        Split a stash of paths. The stash from_stash will be split into two
        stashes depending on the other options passed in. If to_stash is provided,
        the second stash will be written there.

        @param stash_splitter: a function that should take a list of paths and return
                               a tuple of two lists (the two resulting stashes).
        @param stash_ranker: a function that should take a list of paths and return
                             a sorted list of paths. This list will then be split
                             according to "limit".
        @param path_ranker: an alternative to stash_splitter. Paths will be sorted
                            with outputs of this function used as a key. The first
                            "limit" of them will be kept, the rest split off.
        @param limit: for use with path_ranker. The number of paths to keep. Default: 8
        @param from_stash: the stash to split (default: 'active')
        @param to_stash: the stash to write to (default: 'stashed')

        stash_splitter overrides stash_ranker, which in turn overrides path_ranker.
        If no functions are provided, the paths are simply split according to the limit.

        The sort done with path_ranker is ascending.

        @returns the resulting PathGroup
        '''

        limit = 8 if limit is None else limit
        from_stash = 'active' if from_stash is None else from_stash
        to_stash = 'stashed' if to_stash is None else to_stash

        new_stashes = self._copy_stashes()
        old_paths = new_stashes[from_stash]

        if stash_splitter is not None:
            keep, split = stash_splitter(old_paths)
        elif stash_ranker is not None:
            ranked_paths = stash_ranker(old_paths)
            keep, split = ranked_paths[:limit], ranked_paths[limit:]
        elif path_ranker is not None:
            ranked_paths = sorted(old_paths, key=path_ranker)
            keep, split = ranked_paths[:limit], ranked_paths[limit:]
        else:
            keep, split = old_paths[:limit], old_paths[limit:]

        new_stashes[from_stash] = keep
        new_stashes[to_stash] = split if to_stash in new_stashes else new_stashes[to_stash] + split
        return self._successor(new_stashes)

    def step(self, n=None, step_func=None, stash=None, successor_func=None, until=None, check_func=None):
        '''
        Step a stash of paths forward.

        @param n: the number of times to step (default: 1 if "until" is not provided)
        @param step_func: if provided, should be a lambda that takes a PathGroup and
                          returns a PathGroup. Will be called with the PathGroup at
                          at every step.
        @param stash: the name of the stash to step (default: 'active')
        @param successor_func: if provided, this function will be called with a path
                               to get its successors. Otherwise, path.successors will
                               be used.
        @param until: if provided, should be a lambda that takes a PathGroup and returns
                      True or False. Stepping will terminate when it is True.
        @param check_func: if provided, this function will be called to decide whether
                            the current path is errored or not. Path.errored will not be
                            called anymore.

        @returns the resulting PathGroup
        '''
        stash = 'active' if stash is None else stash
        n = n if n is not None else 1 if until is None else 100000
        pg = self

        for i in range(n):
            l.debug("Round %d: stepping %s", i, pg)

            pg = pg._one_step(stash=stash, successor_func=successor_func, check_func=check_func)
            if step_func is not None:
                pg = step_func(pg)

            if len(pg.stashes[stash]) == 0:
                l.debug("Out of paths in stash %s", stash)
                break

            if until is not None and until(pg):
                l.debug("Until function returned true")
                break

        return pg

    def prune(self, filter_func=None, from_stash=None, to_stash=None):
        '''
        Prune unsatisfiable paths from a stash.

        @param filter_func: only prune paths that match this filter
        @param from_stash: prune paths from this stash (default: 'active')
        @param to_stash: put pruned paths in this stash (default: 'pruned')

        @returns the resulting PathGroup
        '''
        to_stash = 'pruned' if to_stash is None else to_stash
        from_stash = 'active' if from_stash is None else from_stash

        to_prune, new_active = self._filter_paths(filter_func, self.stashes[from_stash])
        new_stashes = self._copy_stashes()

        for p in to_prune:
            if p._error is not None or not p.state.satisfiable():
                if to_stash not in new_stashes:
                    new_stashes[to_stash] = [ ]
                new_stashes[to_stash].append(p)
                self._hierarchy.unreachable(p)
            else:
                new_active.append(p)

        new_stashes[from_stash] = new_active
        return self._successor(new_stashes)

    def stash(self, filter_func=None, from_stash=None, to_stash=None):
        '''
        Stash some paths.

        @param filter_func: stash paths that match this filter. Should be a function
                            that takes a path and returns True or False. Default: stash
                            all paths
        @param from_stash: take matching paths from this stash (default: 'active')
        @param to_stash: put matching paths into this stash: (default: 'stashed')

        @returns the resulting PathGroup
        '''
        to_stash = 'stashed' if to_stash is None else to_stash
        from_stash = 'active' if from_stash is None else from_stash

        new_stashes = self._copy_stashes()
        self._move(new_stashes, filter_func, from_stash, to_stash)
        return self._successor(new_stashes)

    def drop(self, filter_func=None, stash=None):
        '''
        Drops paths from a stash.

        @param filter_func: stash paths that match this filter. Should be a function that
                            takes a path and returns True or False. Default:
                            drop all paths
        @param stash: drop matching paths from this stash (default: 'active')

        @returns the resulting PathGroup
        '''
        stash = 'active' if stash is None else stash

        new_stashes = self._copy_stashes()
        if stash in new_stashes:
            dropped, new_stash = self._filter_paths(filter_func, new_stashes[stash])
            new_stashes[stash] = new_stash
        else:
            dropped = [ ]

        l.debug("Dropping %d paths.", len(dropped))
        return self._successor(new_stashes)

    def unstash(self, filter_func=None, to_stash=None, from_stash=None, except_stash=None):
        '''
        Unstash some paths.

        @param filter_func: unstash paths that match this filter. Should be a function
                            that takes a path and returns True or False. Default:
                            unstash all paths
        @param from_stash: take matching paths from this stash (default: 'stashed')
        @param to_stash: put matching paths into this stash: (default: 'active')
        @param except_stash: if provided, unstash all stashes except for this one

        @returns the resulting PathGroup
        '''
        to_stash = 'active' if to_stash is None else to_stash
        from_stash = 'stashed' if from_stash is None else from_stash

        l.debug("Unstashing from stash %s to stash %s", from_stash, to_stash)

        new_stashes = self._copy_stashes()

        for k in new_stashes.keys():
            if k == to_stash: continue
            elif except_stash is not None and k == except_stash: continue
            elif from_stash is not None and k != from_stash: continue

            l.debug("... checking stash %s with %d paths", k, len(new_stashes[k]))
            self._move(new_stashes, filter_func, k, to_stash)

        return self._successor(new_stashes)

    def merge(self, merge_func=None, stash=None):
        '''
        Merge the states in a given stash.

        @param stash: the stash (default: 'active')
        @param merge_func: if provided, instead of using path.merge, call this
                           function with the paths as the argument. Should return
                           the merged path.

        @returns the result PathGroup
        '''
        stash = 'active' if stash is None else stash
        to_merge = self.stashes[stash]
        not_to_merge = [ ]

        merge_groups = [ ]
        while len(to_merge) > 0:
            g, to_merge = self._filter_paths(lambda p: p.addr == to_merge[0].addr, to_merge)
            if len(g) == 1:
                not_to_merge.append(g)
            merge_groups.append(g)

        for g in merge_groups:
            try:
                m = g[0].merge(*g[1:]) if merge_func is None else merge_func(*g)
                not_to_merge.append(m)
            except simuvex.SimMergeError:
                l.warning("SimMergeError while merging %d paths", len(g), exc_info=True)
                not_to_merge.extend(g)

        new_stashes = self._copy_stashes()
        new_stashes[stash] = not_to_merge
        return self._successor(new_stashes)

    #
    # Various canned functionality
    #

    def stash_not_addr(self, addr, from_stash=None, to_stash=None):
        '''
        Stash all paths not at address addr from stash from_stash to stash to_stash.
        '''
        return self.stash(lambda p: p.addr != addr, from_stash=from_stash, to_stash=to_stash)

    def stash_addr(self, addr, from_stash=None, to_stash=None):
        '''
        Stash all paths at address addr from stash from_stash to stash to_stash.
        '''
        return self.stash(lambda p: p.addr == addr, from_stash=from_stash, to_stash=to_stash)

    def stash_addr_past(self, addr, from_stash=None, to_stash=None):
        '''
        Stash all paths containg address addr in their backtrace from stash
        from_stash to stash to_stash.
        '''
        return self.stash(lambda p: addr in p.addr_backtrace, from_stash=from_stash, to_stash=to_stash)

    def stash_not_addr_past(self, addr, from_stash=None, to_stash=None):
        '''
        Stash all paths not containg address addr in their backtrace from stash
        from_stash to stash to_stash.
        '''
        return self.stash(lambda p: addr not in p.addr_backtrace, from_stash=from_stash, to_stash=to_stash)

    def stash_all(self, from_stash=None, to_stash=None):
        '''
        Stash all paths from stash from_stash to stash to_stash.
        '''
        return self.stash(lambda p: True, from_stash=from_stash, to_stash=to_stash)

    def unstash_addr(self, addr, from_stash=None, to_stash=None, except_stash=None):
        '''
        Untash all paths at address addr.
        '''
        return self.unstash(lambda p: p.addr == addr, from_stash=from_stash, to_stash=to_stash, except_stash=except_stash)

    def unstash_addr_past(self, addr, from_stash=None, to_stash=None, except_stash=None):
        '''
        Untash all paths containing address addr in their backtrace.
        '''
        return self.unstash(lambda p: addr in p.addr_backtrace, from_stash=from_stash, to_stash=to_stash, except_stash=except_stash)

    def unstash_not_addr(self, addr, from_stash=None, to_stash=None, except_stash=None):
        '''
        Untash all paths not at address addr.
        '''
        return self.unstash(lambda p: p.addr != addr, from_stash=from_stash, to_stash=to_stash, except_stash=except_stash)

    def unstash_not_addr_past(self, addr, from_stash=None, to_stash=None, except_stash=None):
        '''
        Untash all paths not containing address addr in their backtrace.
        '''
        return self.unstash(lambda p: addr not in p.addr_backtrace, from_stash=from_stash, to_stash=to_stash, except_stash=except_stash)

    def unstash_all(self, from_stash=None, to_stash=None, except_stash=None):
        '''
        Untash all paths.
        '''
        return self.unstash(lambda p: True, from_stash=from_stash, to_stash=to_stash, except_stash=except_stash)

    #
    # High-level functionality
    #

    def explore(self, stash=None, n=None, find=None, avoid=None, num_find=None, found_stash=None, avoid_stash=None):
        '''
        A replacement for the Explorer surveyor. Tick stash "stash" forward (up to n
        times or until num_find paths are found), looking for condition "find",
        avoiding condition "avoid". Stashes found paths into "found_stash' and
        avoided paths into "avoid_stash".
        '''
        find = self._condition_to_lambda(find)
        avoid = self._condition_to_lambda(avoid)
        found_stash = 'found' if found_stash is None else found_stash
        avoid_stash = 'avoid' if avoid_stash is None else avoid_stash
        num_find = 1 if num_find is None else num_find
        cur_found = len(self.stashes[found_stash]) if found_stash in self.stashes else 0

        explore_step_func = lambda pg: pg.stash(find, from_stash=stash, to_stash=found_stash) \
                                         .stash(avoid, from_stash=stash, to_stash=avoid_stash)
        until_func = lambda pg: len(pg.stashes[found_stash]) >= cur_found + num_find
        return self.step(n=n, step_func=explore_step_func, until=until_func, stash=stash)

from .path_hierarchy import PathHierarchy
from .errors import PathUnreachableError
from .path import Path
